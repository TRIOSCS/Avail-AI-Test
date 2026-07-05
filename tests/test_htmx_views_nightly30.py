"""tests/test_htmx_views_nightly30.py — Coverage boost for below-85% HTMX modules.

Targets the seven modules below 85% coverage:
  app/routers/htmx/proactive.py     (57%)
  app/routers/htmx/archive.py       (76%)
  app/routers/htmx/sourcing.py      (77%)
  app/routers/htmx/prospecting.py   (82%)
  app/routers/htmx/buy_plans.py     (82%)
  app/routers/htmx/vendors.py       (81%)

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, db_session, test_user, test_company, etc.)
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Company,
    CustomerSite,
    ProactiveMatch,
    Requirement,
    Requisition,
    RequisitionTask,
    TroubleTicket,
    User,
    VendorCard,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

HX = {"HX-Request": "true"}

_OK_HTML = HTMLResponse("<div>ok</div>")


def _make_prospect(db: Session, name: str = "TestCorp", status: str = "suggested"):
    from app.models.prospect_account import ProspectAccount

    p = ProspectAccount(
        name=name,
        domain=f"{name.lower().replace(' ', '')}.com",
        industry="Aerospace & Defense",
        region="US",
        fit_score=75,
        readiness_score=60,
        status=status,
        discovery_source="explorium",
        readiness_signals={},
        contacts_preview=[],
        similar_customers=[],
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_task(db: Session, user: User, company: Company, title: str = "Follow up") -> RequisitionTask:
    t = RequisitionTask(
        company_id=company.id,
        title=title,
        status="todo",
        task_type="sales",
        created_by=user.id,
        assigned_to_id=user.id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_ticket(db: Session, user: User) -> TroubleTicket:
    from app.constants import TicketSource, TicketStatus

    t = TroubleTicket(
        ticket_number="TKT-0001",
        submitted_by=user.id,
        status=TicketStatus.SUBMITTED,
        source=TicketSource.REPORT_BUTTON,
        title="Test ticket",
        description="Something broke",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _make_sourcing_lead(db: Session, requirement: Requirement) -> "SourcingLead":  # noqa: F821
    import uuid

    from app.models.sourcing_lead import SourcingLead

    lead = SourcingLead(
        lead_id=str(uuid.uuid4()),
        requirement_id=requirement.id,
        requisition_id=requirement.requisition_id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name="Arrow Electronics",
        vendor_name_normalized="arrow electronics",
        primary_source_type="api",
        primary_source_name="brokerbin",
        confidence_score=0.8,
        confidence_band="high",
        reason_summary="Found via API",
        vendor_safety_band="trusted",
        vendor_safety_score=0.9,
        buyer_status="new",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: proactive.py
# ═══════════════════════════════════════════════════════════════════════════════


class TestProactiveListPartial:
    """GET /v2/partials/proactive — proactive_list_partial."""

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_list_matches_tab(self, mock_get, client: TestClient):
        resp = client.get("/v2/partials/proactive", headers=HX)
        assert resp.status_code == 200

    @patch("app.services.proactive_service.get_sent_offers", return_value=[])
    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_list_sent_tab(self, mock_get, mock_sent, client: TestClient):
        resp = client.get("/v2/partials/proactive?tab=sent", headers=HX)
        assert resp.status_code == 200


class TestProactiveRefresh:
    """POST /v2/partials/proactive/refresh — proactive_refresh."""

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch("app.services.proactive_matching.run_proactive_scan")
    def test_refresh_triggers_scan(self, mock_scan, mock_get, client: TestClient):
        resp = client.post("/v2/partials/proactive/refresh", headers=HX)
        assert resp.status_code == 200


class TestProactiveBatchDismiss:
    """POST /v2/partials/proactive/batch-dismiss."""

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_dismiss_no_ids(self, mock_get, client: TestClient):
        resp = client.post("/v2/partials/proactive/batch-dismiss", data={}, headers=HX)
        assert resp.status_code == 200

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    def test_dismiss_with_ids(
        self,
        mock_get,
        client: TestClient,
        db_session: Session,
        test_offer,
        test_requisition,
        test_customer_site,
        test_user: User,
    ):
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=test_requisition.id,
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
            status="new",
        )
        db_session.add(match)
        db_session.commit()

        resp = client.post(
            "/v2/partials/proactive/batch-dismiss",
            data={"match_ids": str(match.id)},
            headers=HX,
        )
        assert resp.status_code == 200


class TestProactivePrepare:
    """POST /v2/proactive/prepare/{site_id}."""

    def test_prepare_no_match_ids_redirects(self, client: TestClient, test_customer_site: CustomerSite):
        resp = client.post(
            f"/v2/proactive/prepare/{test_customer_site.id}",
            data={},
            headers=HX,
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

    def test_prepare_with_match_ids(
        self,
        client: TestClient,
        db_session: Session,
        test_offer,
        test_requisition,
        test_customer_site,
        test_user: User,
    ):
        match = ProactiveMatch(
            offer_id=test_offer.id,
            requirement_id=test_requisition.id,
            requisition_id=test_requisition.id,
            customer_site_id=test_customer_site.id,
            salesperson_id=test_user.id,
            mpn="LM317T",
            status="new",
        )
        db_session.add(match)
        db_session.commit()

        resp = client.post(
            f"/v2/proactive/prepare/{test_customer_site.id}",
            data={"match_ids": str(match.id)},
            headers=HX,
        )
        assert resp.status_code == 200


class TestProactiveSendOffer:
    """POST /v2/proactive/send."""

    def test_send_no_matches_returns_400(self, client: TestClient):
        resp = client.post(
            "/v2/proactive/send",
            data={"match_ids": "", "contact_ids": "1"},
            headers=HX,
        )
        assert resp.status_code == 400

    def test_send_no_contacts_returns_400(self, client: TestClient):
        resp = client.post(
            "/v2/proactive/send",
            data={"match_ids": "1", "contact_ids": ""},
            headers=HX,
        )
        assert resp.status_code == 400

    @patch(
        "app.services.proactive_service.get_matches_for_user",
        return_value={"groups": [], "stats": {"total": 0}},
    )
    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        return_value={"line_items": [], "recipient_emails": []},
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_success(
        self,
        mock_token,
        mock_send,
        mock_get,
        client: TestClient,
    ):
        resp = client.post(
            "/v2/proactive/send",
            data={
                "match_ids": "1",
                "contact_ids": "1",
                "subject": "Test",
                "body": "Hello",
            },
            headers=HX,
        )
        assert resp.status_code == 200

    @patch(
        "app.services.proactive_service.send_proactive_offer",
        new_callable=AsyncMock,
        side_effect=ValueError("bad input"),
    )
    @patch("app.scheduler.get_valid_token", new_callable=AsyncMock, return_value="tok")
    def test_send_value_error_returns_400(self, mock_token, mock_send, client: TestClient):
        resp = client.post(
            "/v2/proactive/send",
            data={"match_ids": "1", "contact_ids": "2"},
            headers=HX,
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: archive.py — trouble tickets + task management
# ═══════════════════════════════════════════════════════════════════════════════


class TestTroubleTicketWorkspace:
    """GET /v2/partials/trouble-tickets/workspace (admin-only)."""

    def test_workspace_renders(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/workspace", headers=HX)
        assert resp.status_code == 200


class TestTroubleTicketList:
    """GET /v2/partials/trouble-tickets/list."""

    def test_list_all(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/list", headers=HX)
        assert resp.status_code == 200

    def test_list_filter_open(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/list?status=open", headers=HX)
        assert resp.status_code == 200

    def test_list_filter_resolved(self, client: TestClient, db_session: Session, test_user: User):
        _make_ticket(db_session, test_user)
        resp = client.get("/v2/partials/trouble-tickets/list?status=resolved", headers=HX)
        assert resp.status_code == 200

    def test_list_filter_arbitrary_status(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/list?status=submitted", headers=HX)
        assert resp.status_code == 200


class TestTroubleTicketDetail:
    """GET /v2/partials/trouble-tickets/{ticket_id}."""

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/trouble-tickets/999999", headers=HX)
        assert resp.status_code == 404

    def test_detail_found(self, client: TestClient, db_session: Session, test_user: User):
        ticket = _make_ticket(db_session, test_user)
        resp = client.get(f"/v2/partials/trouble-tickets/{ticket.id}", headers=HX)
        assert resp.status_code == 200


class TestAccountTasks:
    """Account task CRUD routes in archive.py."""

    @patch("app.services.task_service.get_open_tasks_for_company", return_value=[])
    def test_tasks_list(self, mock_tasks, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tasks", headers=HX)
        assert resp.status_code == 200

    def test_tasks_list_404(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999/tasks", headers=HX)
        assert resp.status_code == 404

    def test_task_add_form(self, client: TestClient, test_company: Company):
        resp = client.get(f"/v2/partials/customers/{test_company.id}/tasks/add-form", headers=HX)
        assert resp.status_code == 200

    def test_task_add_form_404(self, client: TestClient):
        resp = client.get("/v2/partials/customers/99999/tasks/add-form", headers=HX)
        assert resp.status_code == 404

    @patch("app.services.task_service.get_open_tasks_for_company", return_value=[])
    @patch("app.services.task_service.create_company_task")
    def test_create_task(
        self, mock_create, mock_tasks, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": "Call back", "due_at": ""},
            headers=HX,
        )
        assert resp.status_code == 200

    def test_create_task_empty_title(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": ""},
            headers=HX,
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()

    def test_create_task_company_404(self, client: TestClient):
        resp = client.post(
            "/v2/partials/customers/99999/tasks",
            data={"title": "Task"},
            headers=HX,
        )
        assert resp.status_code == 404

    def test_create_task_invalid_date(
        self, client: TestClient, db_session: Session, test_company: Company, test_user: User
    ):
        test_company.account_owner_id = test_user.id
        db_session.commit()
        resp = client.post(
            f"/v2/partials/customers/{test_company.id}/tasks",
            data={"title": "Fix it", "due_at": "not-a-date"},
            headers=HX,
        )
        assert resp.status_code == 200
        assert "invalid date" in resp.text.lower()


class TestTaskActions:
    """POST /v2/partials/tasks/{task_id}/complete and DELETE."""

    def test_complete_task(self, client: TestClient, db_session: Session, test_user: User, test_company: Company):
        task = _make_task(db_session, test_user, test_company)
        resp = client.post(f"/v2/partials/tasks/{task.id}/complete", headers=HX)
        assert resp.status_code == 200

    def test_complete_task_404(self, client: TestClient):
        resp = client.post("/v2/partials/tasks/99999/complete", headers=HX)
        assert resp.status_code == 404

    def test_delete_task(self, client: TestClient, db_session: Session, test_user: User, test_company: Company):
        task = _make_task(db_session, test_user, test_company)
        resp = client.delete(f"/v2/partials/tasks/{task.id}", headers=HX)
        assert resp.status_code == 200

    def test_delete_task_404(self, client: TestClient):
        resp = client.delete("/v2/partials/tasks/99999", headers=HX)
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: sourcing.py — leads, results, workspace
# ═══════════════════════════════════════════════════════════════════════════════


class TestSourcingResultsPartial:
    """GET /v2/partials/sourcing/{requirement_id}."""

    def test_results_empty(self, client: TestClient, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        resp = client.get(f"/v2/partials/sourcing/{req.id}", headers=HX)
        assert resp.status_code == 200

    def test_results_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/99999", headers=HX)
        assert resp.status_code == 404

    def test_results_with_filters(self, client: TestClient, test_requisition: Requisition, db_session: Session):
        req = test_requisition.requirements[0]
        _make_sourcing_lead(db_session, req)
        resp = client.get(
            f"/v2/partials/sourcing/{req.id}?confidence=high&sort=freshest",
            headers=HX,
        )
        assert resp.status_code == 200

    def test_results_sort_safest(self, client: TestClient, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        resp = client.get(f"/v2/partials/sourcing/{req.id}?sort=safest", headers=HX)
        assert resp.status_code == 200

    def test_results_sort_contact(self, client: TestClient, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        resp = client.get(f"/v2/partials/sourcing/{req.id}?sort=contact", headers=HX)
        assert resp.status_code == 200

    def test_results_corroborated_filter(self, client: TestClient, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        resp = client.get(f"/v2/partials/sourcing/{req.id}?corroborated=yes", headers=HX)
        assert resp.status_code == 200

    def test_results_contactability_filter(self, client: TestClient, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        resp = client.get(f"/v2/partials/sourcing/{req.id}?contactability=has_email", headers=HX)
        assert resp.status_code == 200


class TestLeadDetail:
    """GET /v2/partials/sourcing/leads/{lead_id}."""

    def test_lead_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/leads/99999", headers=HX)
        assert resp.status_code == 404

    def test_lead_detail(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        lead = _make_sourcing_lead(db_session, req)
        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)
        assert resp.status_code == 200


class TestLeadStatusUpdate:
    """POST /v2/partials/sourcing/leads/{lead_id}/status."""

    def test_lead_status_not_found(self, client: TestClient):
        resp = client.post(
            "/v2/partials/sourcing/leads/99999/status",
            data={"status": "contacted"},
            headers=HX,
        )
        assert resp.status_code == 404

    def test_lead_status_update(
        self,
        client: TestClient,
        db_session: Session,
        test_requisition: Requisition,
    ):
        req = test_requisition.requirements[0]
        lead = _make_sourcing_lead(db_session, req)
        resp = client.post(
            f"/v2/partials/sourcing/leads/{lead.id}/status",
            data={"status": "contacted", "context": "results"},
            headers=HX,
        )
        assert resp.status_code == 200


class TestSourcingLeadPanel:
    """GET /v2/partials/sourcing/leads/{lead_id}/panel."""

    def test_panel_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/sourcing/leads/99999/panel", headers=HX)
        assert resp.status_code == 404

    def test_panel_found(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        req = test_requisition.requirements[0]
        lead = _make_sourcing_lead(db_session, req)
        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}/panel", headers=HX)
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: prospecting.py
# ═══════════════════════════════════════════════════════════════════════════════


class TestProspectingList:
    """GET /v2/partials/prospecting."""

    def test_list_empty(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting", headers=HX)
        assert resp.status_code == 200

    def test_list_with_status_filter(self, client: TestClient, db_session: Session):
        _make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting?status=suggested&sort=fit_desc", headers=HX)
        assert resp.status_code == 200

    def test_list_buyer_ready_sort(self, client: TestClient, db_session: Session):
        _make_prospect(db_session)
        resp = client.get("/v2/partials/prospecting?sort=buyer_ready_desc", headers=HX)
        assert resp.status_code == 200

    def test_list_with_query(self, client: TestClient, db_session: Session):
        _make_prospect(db_session, name="Acme Corp")
        resp = client.get("/v2/partials/prospecting?q=Acme&sort=recent_desc", headers=HX)
        assert resp.status_code == 200


class TestProspectingDetail:
    """GET /v2/partials/prospecting/{prospect_id}."""

    def test_detail_404(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/99999", headers=HX)
        assert resp.status_code == 404

    def test_detail_found(self, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        resp = client.get(f"/v2/partials/prospecting/{p.id}", headers=HX)
        assert resp.status_code == 200


class TestProspectingClaim:
    """POST /v2/partials/prospecting/{prospect_id}/claim."""

    @patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock)
    @patch("app.services.prospect_claim.claim_prospect")
    def test_claim_success(self, mock_claim, mock_enrich, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        resp = client.post(f"/v2/partials/prospecting/{p.id}/claim", data={}, headers=HX)
        assert resp.status_code == 200

    def test_claim_not_found(self, client: TestClient):
        with patch("app.services.prospect_claim.claim_prospect", side_effect=LookupError):
            resp = client.post("/v2/partials/prospecting/99999/claim", data={}, headers=HX)
        assert resp.status_code == 404

    @patch("app.services.prospect_claim.trigger_deep_enrichment_bg", new_callable=AsyncMock)
    @patch("app.services.prospect_claim.claim_prospect", side_effect=ValueError("already claimed"))
    def test_claim_value_error(self, mock_claim, mock_enrich, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        resp = client.post(f"/v2/partials/prospecting/{p.id}/claim", data={}, headers=HX)
        assert resp.status_code == 200  # Shows error in UI, not 4xx


class TestProspectingDismiss:
    """POST /v2/partials/prospecting/{prospect_id}/dismiss."""

    def test_dismiss_suggested(self, client: TestClient, db_session: Session):
        p = _make_prospect(db_session, status="suggested")
        resp = client.post(
            f"/v2/partials/prospecting/{p.id}/dismiss",
            data={"reason": "not a fit"},
            headers=HX,
        )
        assert resp.status_code == 200
        db_session.refresh(p)
        assert p.status == "dismissed"

    def test_dismiss_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/99999/dismiss", data={}, headers=HX)
        assert resp.status_code == 404

    def test_dismiss_non_suggested(self, client: TestClient, db_session: Session):
        p = _make_prospect(db_session, status="claimed")
        resp = client.post(
            f"/v2/partials/prospecting/{p.id}/dismiss",
            data={"reason": "not a fit"},
            headers=HX,
        )
        assert resp.status_code == 200  # Error shown in UI


class TestProspectingRelease:
    """POST /v2/partials/prospecting/{prospect_id}/release."""

    @patch("app.services.prospect_claim.release_prospect")
    def test_release(self, mock_release, client: TestClient, db_session: Session):
        p = _make_prospect(db_session, status="claimed")
        resp = client.post(f"/v2/partials/prospecting/{p.id}/release", data={}, headers=HX)
        assert resp.status_code == 200

    def test_release_not_found(self, client: TestClient):
        with patch("app.services.prospect_claim.release_prospect", side_effect=LookupError):
            resp = client.post("/v2/partials/prospecting/99999/release", data={}, headers=HX)
        assert resp.status_code == 404

    @patch("app.services.prospect_claim.release_prospect", side_effect=ValueError("not allowed"))
    def test_release_value_error(self, mock_release, client: TestClient, db_session: Session):
        p = _make_prospect(db_session, status="claimed")
        resp = client.post(f"/v2/partials/prospecting/{p.id}/release", data={}, headers=HX)
        assert resp.status_code == 200


class TestProspectingEnrich:
    """POST /v2/partials/prospecting/{prospect_id}/enrich."""

    @patch("app.services.prospect_free_enrichment.run_enrichment_job", new_callable=AsyncMock)
    def test_enrich_kicks_off(self, mock_run, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich", data={}, headers=HX)
        assert resp.status_code == 200

    def test_enrich_already_running(self, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        p.enrichment_data = {"enrich_status": "running"}
        db_session.commit()
        resp = client.post(f"/v2/partials/prospecting/{p.id}/enrich", data={}, headers=HX)
        assert resp.status_code == 200

    def test_enrich_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/prospecting/99999/enrich", data={}, headers=HX)
        assert resp.status_code == 404


class TestProspectingEnrichStatus:
    """GET /v2/partials/prospecting/{prospect_id}/enrich-status."""

    def test_status_done(self, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        resp = client.get(f"/v2/partials/prospecting/{p.id}/enrich-status", headers=HX)
        # 286 = htmx stop-polling status
        assert resp.status_code in (200, 286)

    def test_status_running(self, client: TestClient, db_session: Session):
        p = _make_prospect(db_session)
        p.enrichment_data = {"enrich_status": "running", "enrich_started_at": "2099-01-01T00:00:00"}
        db_session.commit()
        resp = client.get(f"/v2/partials/prospecting/{p.id}/enrich-status", headers=HX)
        assert resp.status_code == 200

    def test_status_not_found_returns_286(self, client: TestClient):
        resp = client.get("/v2/partials/prospecting/99999/enrich-status", headers=HX)
        assert resp.status_code == 286


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: buy_plans.py — list/detail/state endpoints
# ═══════════════════════════════════════════════════════════════════════════════


class TestBuyPlanListViews:
    """GET /v2/partials/buy-plans and sub-tabs."""

    def test_buy_plans_list(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans", headers=HX)
        assert resp.status_code == 200

    def test_approvals_list(self, client: TestClient):
        resp = client.get("/v2/partials/approvals", headers=HX)
        assert resp.status_code == 200


class TestBuyPlanDetail:
    """GET /v2/partials/buy-plans/{plan_id}."""

    def test_detail_not_found(self, client: TestClient):
        resp = client.get("/v2/partials/buy-plans/99999", headers=HX)
        assert resp.status_code == 404

    def test_detail_found(self, client: TestClient, test_buy_plan):
        resp = client.get(f"/v2/partials/buy-plans/{test_buy_plan.id}", headers=HX)
        assert resp.status_code == 200


class TestBuyPlanSubmit:
    """POST /v2/partials/buy-plans/{plan_id}/submit."""

    def test_submit_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/buy-plans/99999/submit", headers=HX)
        assert resp.status_code == 404

    def test_submit_draft_plan(self, client: TestClient, test_buy_plan):
        resp = client.post(f"/v2/partials/buy-plans/{test_buy_plan.id}/submit", headers=HX)
        assert resp.status_code in (200, 400, 403)


class TestBuyPlanCancel:
    """POST /v2/partials/buy-plans/{plan_id}/cancel."""

    def test_cancel_not_found(self, client: TestClient):
        resp = client.post("/v2/partials/buy-plans/99999/cancel", headers=HX)
        assert resp.status_code == 404

    def test_cancel_plan(self, client: TestClient, test_buy_plan):
        resp = client.post(f"/v2/partials/buy-plans/{test_buy_plan.id}/cancel", headers=HX)
        assert resp.status_code in (200, 400, 403)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: vendors.py — uncovered vendor routes
# ═══════════════════════════════════════════════════════════════════════════════


class TestVendorOwnership:
    """GET /v2/partials/vendors/{vendor_id}/ownership."""

    def test_ownership(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/ownership", headers=HX)
        assert resp.status_code == 200

    def test_ownership_404(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/ownership", headers=HX)
        assert resp.status_code == 404


class TestVendorReviews:
    """Vendor review CRUD."""

    def test_list_reviews(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/reviews", headers=HX)
        assert resp.status_code == 200

    def test_list_reviews_404(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/99999/reviews", headers=HX)
        assert resp.status_code == 404

    def test_create_review(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/reviews",
            data={"rating": "4", "body": "Good service"},
            headers=HX,
        )
        assert resp.status_code == 200


class TestVendorClaimRelease:
    """Vendor claim/release/archive/unarchive."""

    def test_claim(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/claim", headers=HX)
        assert resp.status_code == 200

    def test_claim_404(self, client: TestClient):
        resp = client.post("/v2/partials/vendors/99999/claim", headers=HX)
        assert resp.status_code == 404

    def test_release(self, client: TestClient, test_vendor_card: VendorCard, db_session: Session, test_user: User):
        from app.models.strategic import StrategicVendor

        sv = StrategicVendor(
            user_id=test_user.id,
            vendor_card_id=test_vendor_card.id,
            expires_at=datetime(2099, 12, 31, tzinfo=timezone.utc),
        )
        db_session.add(sv)
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/release", headers=HX)
        assert resp.status_code == 200

    def test_archive_vendor(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/archive", headers=HX)
        assert resp.status_code == 200

    def test_unarchive_vendor(self, client: TestClient, test_vendor_card: VendorCard, db_session: Session):
        test_vendor_card.is_archived = True
        db_session.commit()
        resp = client.post(f"/v2/partials/vendors/{test_vendor_card.id}/unarchive", headers=HX)
        assert resp.status_code == 200
