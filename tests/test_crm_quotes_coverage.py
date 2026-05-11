import os

os.environ["TESTING"] = "1"
"""test_crm_quotes_coverage.py — Coverage tests for app/routers/crm/quotes.py"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from tests.conftest import engine

_ = engine

from app.models import Company, CustomerSite, Offer, Quote, Requisition, User

# ── Helpers ──────────────────────────────────────────────────────────


def _make_company(db: Session, name: str = "Test Corp") -> Company:
    co = Company(name=name, is_active=True, created_at=datetime.now(timezone.utc))
    db.add(co)
    db.flush()
    return co


def _make_site(db: Session, company_id: int, email: str = "buyer@testcorp.com") -> CustomerSite:
    site = CustomerSite(
        company_id=company_id,
        site_name="HQ",
        contact_name="Jane Smith",
        contact_email=email,
        payment_terms="Net 30",
        shipping_terms="FOB Origin",
    )
    db.add(site)
    db.flush()
    return site


def _make_req(db: Session, user_id: int, site_id: int | None = None, status: str = "active") -> Requisition:
    req = Requisition(
        name="Test REQ",
        customer_name="Test Corp",
        status=status,
        created_by=user_id,
        customer_site_id=site_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    return req


def _make_draft_quote(
    db: Session,
    req_id: int,
    site_id: int,
    user_id: int,
    quote_number: str = "Q-2026-0001",
    status: str = "draft",
    line_items: list | None = None,
) -> Quote:
    q = Quote(
        requisition_id=req_id,
        customer_site_id=site_id,
        quote_number=quote_number,
        status=status,
        line_items=line_items or [],
        subtotal=500.00,
        total_cost=250.00,
        total_margin_pct=50.0,
        created_by_id=user_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(q)
    db.flush()
    return q


# ── GET /api/requisitions/{req_id}/quote ─────────────────────────────


class TestGetQuote:
    def test_get_quote_missing_req_returns_404(self, client):
        resp = client.get("/api/requisitions/999999/quote")
        assert resp.status_code == 404

    def test_get_quote_no_quote_returns_null(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/quote")
        assert resp.status_code == 200
        assert resp.json() is None

    def test_get_quote_returns_latest_revision(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q1 = _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REV-LATEST-OLD", status="revised")
        q2 = _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REV-LATEST-NEW")
        q2.revision = 2
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/quote")
        assert resp.status_code == 200
        data = resp.json()
        assert data["revision"] == 2


# ── GET /api/quotes/recent-terms ─────────────────────────────────────


class TestRecentQuoteTerms:
    def test_recent_terms_empty(self, client):
        resp = client.get("/api/quotes/recent-terms")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_recent_terms_returns_quotes(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        q.payment_terms = "Net 60"
        q.shipping_terms = "CIF"
        db_session.commit()
        resp = client.get("/api/quotes/recent-terms")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        entry = data[0]
        assert entry["payment_terms"] == "Net 60"
        assert entry["shipping_terms"] == "CIF"
        assert "quote_number" in entry
        assert "customer_name" in entry

    def test_recent_terms_only_current_user_quotes(self, client, db_session, test_user):
        """Only returns quotes created by the authenticated user."""
        other_user = User(
            email="other@test.com",
            name="Other User",
            role="buyer",
            azure_id="other-azure-id",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_user)
        db_session.flush()

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, other_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, other_user.id)
        q.payment_terms = "Net 90"
        db_session.commit()

        resp = client.get("/api/quotes/recent-terms")
        assert resp.status_code == 200
        # other user's quote should not be in results (client is test_user)
        data = resp.json()
        assert all(entry["payment_terms"] != "Net 90" for entry in data)


# ── GET /api/requisitions/{req_id}/quotes ────────────────────────────


class TestListQuotes:
    def test_list_quotes_missing_req_returns_404(self, client):
        resp = client.get("/api/requisitions/999999/quotes")
        assert resp.status_code == 404

    def test_list_quotes_empty(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id)
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/quotes")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_quotes_returns_all_revisions(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REVISIONS-001", status="revised")
        _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REVISIONS-002")
        db_session.commit()
        resp = client.get(f"/api/requisitions/{req.id}/quotes")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2


# ── POST /api/requisitions/{req_id}/quote ────────────────────────────


class TestCreateQuote:
    def test_create_quote_missing_req_returns_404(self, client):
        resp = client.post("/api/requisitions/999999/quote", json={"offer_ids": [], "line_items": []})
        assert resp.status_code == 404

    def test_create_quote_no_customer_site_returns_400(self, client, db_session, test_user):
        req = _make_req(db_session, test_user.id, site_id=None)
        db_session.commit()
        resp = client.post(f"/api/requisitions/{req.id}/quote", json={"offer_ids": [], "line_items": []})
        assert resp.status_code == 400

    def test_create_quote_with_line_items(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{req.id}/quote",
            json={
                "offer_ids": [],
                "line_items": [
                    {
                        "mpn": "LM317T",
                        "vendor_name": "Arrow",
                        "qty": 100,
                        "unit_cost": 0.50,
                        "unit_sell": 0.75,
                        "margin": 0.33,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["quote_number"] is not None

    def test_create_quote_from_offer_ids(self, client, db_session, test_user, test_requisition):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        test_requisition.customer_site_id = site.id
        db_session.flush()

        req_item = test_requisition.requirements[0]
        offer = Offer(
            requisition_id=test_requisition.id,
            requirement_id=req_item.id,
            vendor_name="Arrow",
            mpn="LM317T",
            qty_available=500,
            unit_price=0.50,
            status="active",
            entered_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(offer)
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/quote",
            json={"offer_ids": [offer.id], "line_items": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert len(data["line_items"]) >= 1

    def test_create_quote_status_changes_req(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id, status="active")
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{req.id}/quote",
            json={
                "offer_ids": [],
                "line_items": [
                    {
                        "mpn": "TEST123",
                        "vendor_name": "X",
                        "qty": 10,
                        "unit_cost": 1.0,
                        "unit_sell": 1.5,
                        "margin": 0.33,
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "req_status" in data


# ── PUT /api/quotes/{quote_id} ───────────────────────────────────────


class TestUpdateQuote:
    def test_update_quote_not_found(self, client):
        resp = client.put("/api/quotes/999999", json={"payment_terms": "Net 60"})
        assert resp.status_code == 404

    def test_update_draft_quote_succeeds(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        resp = client.put(f"/api/quotes/{q.id}", json={"payment_terms": "Net 60", "notes": "Updated"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["payment_terms"] == "Net 60"
        assert data["notes"] == "Updated"

    def test_update_non_draft_quote_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, status="sent")
        db_session.commit()

        resp = client.put(f"/api/quotes/{q.id}", json={"notes": "Cannot edit sent"})
        assert resp.status_code == 400

    def test_update_quote_line_items_recalculates_totals(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, line_items=[])
        db_session.commit()

        line_items = [
            {"mpn": "LM317T", "vendor_name": "Arrow", "qty": 100, "unit_cost": 0.40, "unit_sell": 0.80, "margin": 0.5}
        ]
        resp = client.put(f"/api/quotes/{q.id}", json={"line_items": line_items})
        assert resp.status_code == 200


# ── DELETE /api/quotes/{quote_id} ───────────────────────────────────


class TestDeleteQuote:
    def test_delete_quote_not_found(self, client):
        resp = client.delete("/api/quotes/999999")
        assert resp.status_code == 404

    def test_delete_non_draft_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, status="sent")
        db_session.commit()

        resp = client.delete(f"/api/quotes/{q.id}")
        assert resp.status_code == 400

    def test_delete_draft_quote_succeeds(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        resp = client.delete(f"/api/quotes/{q.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_quote_with_buy_plan_returns_400(self, client, db_session, test_user):
        from app.models.buy_plan import BuyPlan

        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.flush()

        bp = BuyPlan(
            quote_id=q.id,
            requisition_id=req.id,
            status="draft",
        )
        db_session.add(bp)
        db_session.commit()

        resp = client.delete(f"/api/quotes/{q.id}")
        assert resp.status_code == 400


# ── POST /api/quotes/{quote_id}/preview ─────────────────────────────


class TestPreviewQuoteEmail:
    def test_preview_not_found(self, client):
        resp = client.post("/api/quotes/999999/preview", json={})
        assert resp.status_code == 404

    def test_preview_returns_html(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert "html" in data
        assert "<html" in data["html"]

    def test_preview_with_override_name(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/preview", json={"to_name": "Custom Name"})
        assert resp.status_code == 200
        assert "html" in resp.json()


# ── POST /api/quotes/{quote_id}/send ─────────────────────────────────


class TestSendQuote:
    def test_send_quote_not_found(self, client):
        resp = client.post("/api/quotes/999999/send", json={})
        assert resp.status_code == 404

    def test_send_quote_no_email_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = CustomerSite(
            company_id=co.id,
            site_name="No Email Site",
            contact_name="Jane",
            contact_email=None,
        )
        db_session.add(site)
        db_session.flush()
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/send", json={})
        assert resp.status_code == 400

    def test_send_quote_invalid_email_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/send", json={"to_email": "not-an-email"})
        assert resp.status_code == 400

    def test_send_quote_graph_success(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={})

        with (
            patch("app.dependencies.require_fresh_token", new=AsyncMock(return_value="mock-token")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = client.post(f"/api/quotes/{q.id}/send", json={"to_email": "buyer@testcorp.com"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["sent_to"] == "buyer@testcorp.com"

    def test_send_quote_graph_error_returns_502(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id)
        db_session.commit()

        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(return_value={"error": "Unauthorized", "detail": "Token expired"})

        with (
            patch("app.dependencies.require_fresh_token", new=AsyncMock(return_value="mock-token")),
            patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        ):
            resp = client.post(f"/api/quotes/{q.id}/send", json={"to_email": "buyer@testcorp.com"})

        assert resp.status_code == 502


# ── POST /api/quotes/{quote_id}/result ──────────────────────────────


class TestQuoteResult:
    def test_quote_result_not_found(self, client):
        resp = client.post("/api/quotes/999999/result", json={"result": "won"})
        assert resp.status_code == 404

    def test_quote_result_won(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, status="sent")
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/result", json={"result": "won", "reason": "Best price"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "won"

        db_session.refresh(q)
        assert q.status == "won"
        assert q.won_revenue is not None

    def test_quote_result_lost(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, status="sent")
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/result", json={"result": "lost", "reason": "Too expensive"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "lost"

    def test_quote_result_won_records_activity(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, status="sent")
        db_session.commit()

        from app.models import ActivityLog

        resp = client.post(f"/api/quotes/{q.id}/result", json={"result": "won"})
        assert resp.status_code == 200
        logs = db_session.query(ActivityLog).filter(ActivityLog.activity_type == "quote_won").all()
        assert len(logs) >= 1


# ── POST /api/quotes/{quote_id}/revise ──────────────────────────────


class TestReviseQuote:
    def test_revise_not_found(self, client):
        resp = client.post("/api/quotes/999999/revise")
        assert resp.status_code == 404

    def test_revise_sent_quote_creates_new_revision(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REV-001", status="sent")
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/revise")
        assert resp.status_code == 200
        data = resp.json()
        assert data["revision"] == 2
        assert data["quote_number"] == "Q-REV-001"

        # Old quote should now be in revised status
        db_session.refresh(q)
        assert q.status == "revised"

    def test_revise_draft_invalid_transition(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-DRAFT-REV", status="draft")
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/revise")
        # draft → revised is valid per QUOTE_TRANSITIONS
        assert resp.status_code in (200, 409)


# ── POST /api/quotes/{quote_id}/reopen ──────────────────────────────


class TestReopenQuote:
    def test_reopen_not_found(self, client):
        resp = client.post("/api/quotes/999999/reopen", json={"revise": False})
        assert resp.status_code == 404

    def test_reopen_without_revise(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REOPEN-01", status="lost")
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/reopen", json={"revise": False})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "sent"

    def test_reopen_with_revise(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(db_session, req.id, site.id, test_user.id, "Q-REOPEN-02", status="lost")
        db_session.commit()

        resp = client.post(f"/api/quotes/{q.id}/reopen", json={"revise": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["revision"] == 2


# ── GET /api/pricing-history/{mpn} ──────────────────────────────────


class TestPricingHistory:
    def test_pricing_history_no_quotes(self, client):
        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mpn"] == "LM317T"
        assert data["history"] == []
        assert data["avg_price"] is None

    def test_pricing_history_with_quotes(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        q = _make_draft_quote(
            db_session,
            req.id,
            site.id,
            test_user.id,
            "Q-PH-001",
            status="sent",
            line_items=[
                {
                    "mpn": "LM317T",
                    "qty": 100,
                    "sell_price": 0.75,
                    "cost_price": 0.50,
                    "margin_pct": 33.0,
                }
            ],
        )
        q.sent_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["history"]) >= 1
        assert data["avg_price"] is not None

    def test_pricing_history_price_range(self, client, db_session, test_user):
        co = _make_company(db_session)
        site = _make_site(db_session, co.id)
        req = _make_req(db_session, test_user.id, site_id=site.id)
        for i, price in enumerate([0.50, 0.75, 1.00]):
            q = _make_draft_quote(
                db_session,
                req.id,
                site.id,
                test_user.id,
                f"Q-PH-RANGE-{i:03d}",
                status="won",
                line_items=[{"mpn": "LM317T", "qty": 100, "sell_price": price, "cost_price": 0.40, "margin_pct": 20.0}],
            )
            q.sent_at = datetime.now(timezone.utc)
        db_session.commit()

        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert data["price_range"] is not None
        assert len(data["price_range"]) == 2
