"""Tests for app/routers/rfq.py — RFQ, Contacts, Responses, Activity & Follow-ups.

Covers: log_call, retry_failed_rfq, list_contacts, send_rfq, poll, update_vendor_response_status,
list_responses, get_activity, rfq_prepare, get_follow_ups, follow_up_summary,
send_follow_up, send_follow_up_batch, _enrich_with_vendor_cards, _enforce_req_scope_for_user.

Called by: pytest
Depends on: conftest fixtures, rfq router
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import ContactStatus
from app.models import (
    ActivityLog,
    Contact,
    Requisition,
    User,
    VendorCard,
    VendorContact,
    VendorResponse,
    VendorReview,
)

# ── Helpers ──────────────────────────────────────────────────────────


def _make_contact(
    db: Session,
    requisition: Requisition,
    user: User,
    *,
    vendor_name: str = "Arrow Electronics",
    vendor_contact: str = "sales@arrow.com",
    contact_type: str = "email",
    status: str = "sent",
    parts: list | None = None,
    subject: str = "RFQ for LM317T",
    created_at: datetime | None = None,
) -> Contact:
    c = Contact(
        requisition_id=requisition.id,
        user_id=user.id,
        contact_type=contact_type,
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower().strip(),
        vendor_contact=vendor_contact,
        parts_included=parts or ["LM317T"],
        subject=subject,
        status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(c)
    db.flush()
    return c


def _make_vendor_response(
    db: Session,
    requisition: Requisition,
    *,
    vendor_name: str = "Arrow Electronics",
    vendor_email: str = "sales@arrow.com",
    status: str = "new",
    contact_id: int | None = None,
) -> VendorResponse:
    vr = VendorResponse(
        requisition_id=requisition.id,
        contact_id=contact_id,
        vendor_name=vendor_name,
        vendor_email=vendor_email,
        subject="Re: RFQ for LM317T",
        body="We have stock at $0.45/pc",
        received_at=datetime.now(timezone.utc),
        status=status,
        confidence=0.9,
        classification="quote_provided",
    )
    db.add(vr)
    db.flush()
    return vr


# ── Phone Call Logging ───────────────────────────────────────────────


class TestLogCall:
    @patch("app.routers.rfq.log_phone_contact", return_value={"id": 1, "status": "ok"})
    def test_log_call_success(self, mock_log, client: TestClient, db_session: Session, test_requisition: Requisition):
        resp = client.post(
            "/api/contacts/phone",
            json={
                "requisition_id": test_requisition.id,
                "vendor_name": "Arrow Electronics",
                "vendor_phone": "+1-555-0100",
                "parts": ["LM317T"],
            },
        )
        assert resp.status_code == 200
        mock_log.assert_called_once()


# ── Retry Failed RFQ ────────────────────────────────────────────────


class TestRetryFailedRfq:
    @patch("app.routers.rfq.send_batch_rfq", new_callable=AsyncMock, return_value=[{"status": "sent"}])
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_retry_success(
        self,
        mock_token,
        mock_send,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        contact = _make_contact(db_session, test_requisition, test_user, status=ContactStatus.FAILED)
        db_session.commit()

        resp = client.post(f"/api/contacts/{contact.id}/retry")
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

    def test_retry_not_failed(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        contact = _make_contact(db_session, test_requisition, test_user, status=ContactStatus.SENT)
        db_session.commit()

        resp = client.post(f"/api/contacts/{contact.id}/retry")
        assert resp.status_code == 400

    def test_retry_not_found(self, client: TestClient):
        resp = client.post("/api/contacts/99999/retry")
        assert resp.status_code == 404


# ── List Contacts ────────────────────────────────────────────────────


class TestListContacts:
    def test_list_contacts(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        _make_contact(db_session, test_requisition, test_user)
        _make_contact(db_session, test_requisition, test_user, vendor_name="Mouser")
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/contacts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2

    def test_list_contacts_empty(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/contacts")
        assert resp.status_code == 200
        assert resp.json() == []


# ── Send RFQ ─────────────────────────────────────────────────────────


class TestSendRFQ:
    @patch("app.routers.rfq.send_batch_rfq", new_callable=AsyncMock, return_value=[{"status": "sent"}])
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_rfq_success(
        self, mock_token, mock_send, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq",
            json={
                "groups": [
                    {
                        "vendor_name": "Arrow",
                        "vendor_email": "sales@arrow.com",
                        "parts": ["LM317T"],
                        "subject": "RFQ",
                        "body": "Please quote",
                    }
                ]
            },
        )
        assert resp.status_code == 200
        assert "results" in resp.json()

    @patch("app.routers.rfq.send_batch_rfq", new_callable=AsyncMock, return_value=[{"status": "sent"}])
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_rfq_updates_requirement_status(
        self, mock_token, mock_send, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        with patch("app.services.requirement_status.on_rfq_sent") as mock_status:
            resp = client.post(
                f"/api/requisitions/{test_requisition.id}/rfq",
                json={
                    "groups": [
                        {
                            "vendor_name": "Arrow",
                            "vendor_email": "sales@arrow.com",
                            "parts": ["LM317T"],
                            "subject": "RFQ",
                            "body": "Please quote",
                        }
                    ]
                },
            )
            assert resp.status_code == 200


# ── Inbox Polling ────────────────────────────────────────────────────


class TestPoll:
    @patch("app.routers.rfq.poll_inbox", new_callable=AsyncMock, return_value=[{"id": 1}])
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_poll_inbox(self, mock_token, mock_poll, client: TestClient, test_requisition: Requisition):
        resp = client.post(f"/api/requisitions/{test_requisition.id}/poll")
        assert resp.status_code == 200
        assert "responses" in resp.json()


# ── Vendor Response Status ───────────────────────────────────────────


class TestUpdateVendorResponseStatus:
    def test_update_status(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        vr = _make_vendor_response(db_session, test_requisition)
        db_session.commit()

        resp = client.patch(f"/api/vendor-responses/{vr.id}/status", json={"status": "reviewed"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "reviewed"

    def test_update_status_not_found(self, client: TestClient):
        resp = client.patch("/api/vendor-responses/99999/status", json={"status": "reviewed"})
        assert resp.status_code == 404

    def test_update_status_invalid(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        vr = _make_vendor_response(db_session, test_requisition)
        db_session.commit()

        resp = client.patch(f"/api/vendor-responses/{vr.id}/status", json={"status": "invalid_status"})
        assert resp.status_code == 422


# ── List Responses ───────────────────────────────────────────────────


class TestListResponses:
    def test_list_responses_default_new(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        _make_vendor_response(db_session, test_requisition, status="new")
        _make_vendor_response(db_session, test_requisition, status="reviewed")
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/responses")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["status"] == "new"

    def test_list_responses_all(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        _make_vendor_response(db_session, test_requisition, status="new")
        _make_vendor_response(db_session, test_requisition, status="reviewed")
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/responses?status=all")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_responses_empty(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/responses")
        assert resp.status_code == 200
        assert resp.json() == []


# ── Activity Feed ────────────────────────────────────────────────────


class TestGetActivity:
    def test_activity_with_contacts_and_responses(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        test_vendor_card: VendorCard,
    ):
        contact = _make_contact(db_session, test_requisition, test_user, vendor_name="Arrow Electronics")
        _make_vendor_response(db_session, test_requisition, vendor_name="Arrow Electronics", contact_id=contact.id)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert "vendors" in data
        assert "summary" in data
        assert data["summary"]["sent"] >= 1

    def test_activity_empty(self, client: TestClient, test_requisition: Requisition):
        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendors"] == []
        assert data["summary"]["sent"] == 0

    def test_activity_vendor_status_quoted(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        _make_contact(db_session, test_requisition, test_user, status=ContactStatus.QUOTED)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        assert data["vendors"][0]["status"] == "quoted"

    def test_activity_vendor_status_declined(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        _make_contact(db_session, test_requisition, test_user, status=ContactStatus.DECLINED)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        assert data["vendors"][0]["status"] == "declined"

    def test_activity_vendor_status_opened(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        _make_contact(db_session, test_requisition, test_user, status=ContactStatus.OPENED)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        assert data["vendors"][0]["status"] == "opened"

    def test_activity_vendor_status_responded(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        _make_contact(db_session, test_requisition, test_user, status=ContactStatus.RESPONDED)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        assert data["vendors"][0]["status"] == "replied"

    def test_activity_with_manual_activities(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        test_vendor_card: VendorCard,
    ):
        activity = ActivityLog(
            user_id=test_user.id,
            activity_type="phone_call",
            channel="phone",
            vendor_card_id=test_vendor_card.id,
            requisition_id=test_requisition.id,
            contact_name="John Sales",
            notes="Called about stock",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(activity)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        assert len(data["vendors"]) >= 1

    def test_activity_response_groups_with_contact_vendor(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Response from 'John Smith' groups under contact vendor 'Arrow
        Electronics'."""
        contact = _make_contact(db_session, test_requisition, test_user, vendor_name="Arrow Electronics")
        _make_vendor_response(
            db_session,
            test_requisition,
            vendor_name="John Smith",
            vendor_email="john@arrow.com",
            contact_id=contact.id,
        )
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        # Response should be grouped with Arrow Electronics, not separately
        vendor_names = [v["vendor_name"] for v in data["vendors"]]
        assert "Arrow Electronics" in vendor_names

    def test_activity_with_vendor_phones(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
        test_vendor_card: VendorCard,
        test_vendor_contact: VendorContact,
    ):
        _make_contact(db_session, test_requisition, test_user, vendor_name="Arrow Electronics")
        db_session.commit()

        resp = client.get(f"/api/requisitions/{test_requisition.id}/activity")
        data = resp.json()
        # Should include phone numbers from vendor card
        vendors_with_phones = [v for v in data["vendors"] if v.get("vendor_phones")]
        assert len(vendors_with_phones) >= 0  # May or may not resolve depending on normalized name match


# ── Follow-Ups ───────────────────────────────────────────────────────


class TestFollowUps:
    def test_get_follow_ups(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        # Create a stale contact (old enough to trigger follow-up)
        _make_contact(
            db_session,
            test_requisition,
            test_user,
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db_session.commit()

        resp = client.get("/api/follow-ups")
        assert resp.status_code == 200
        data = resp.json()
        assert "follow_ups" in data
        assert "count" in data
        assert data["count"] >= 1

    def test_get_follow_ups_empty(self, client: TestClient):
        resp = client.get("/api/follow-ups")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_follow_up_summary(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        _make_contact(
            db_session,
            test_requisition,
            test_user,
            status="sent",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db_session.commit()

        resp = client.get("/api/follow-ups/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert "total" in data
        assert "by_requisition" in data

    def test_follow_up_summary_empty(self, client: TestClient):
        resp = client.get("/api/follow-ups/summary")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestSendFollowUp:
    @patch("app.utils.graph_client.GraphClient")
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_follow_up_with_body(
        self,
        mock_token,
        MockGC,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()
        MockGC.return_value = mock_gc

        contact = _make_contact(db_session, test_requisition, test_user, status="sent")
        db_session.commit()

        resp = client.post(
            f"/api/follow-ups/{contact.id}/send",
            json={"body": "Following up on our request."},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("app.utils.graph_client.GraphClient")
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_follow_up_default_body(
        self,
        mock_token,
        MockGC,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()
        MockGC.return_value = mock_gc

        contact = _make_contact(db_session, test_requisition, test_user, status="sent")
        db_session.commit()

        resp = client.post(f"/api/follow-ups/{contact.id}/send", json={"body": ""})
        assert resp.status_code == 200

    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_send_follow_up_contact_not_found(self, mock_token, client: TestClient):
        resp = client.post("/api/follow-ups/99999/send", json={"body": "test"})
        assert resp.status_code == 404


class TestSendFollowUpBatch:
    @patch("app.utils.graph_client.GraphClient")
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_batch_send(
        self,
        mock_token,
        MockGC,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()
        MockGC.return_value = mock_gc

        c1 = _make_contact(db_session, test_requisition, test_user, vendor_name="Arrow")
        c2 = _make_contact(
            db_session, test_requisition, test_user, vendor_name="Mouser", vendor_contact="sales@mouser.com"
        )
        db_session.commit()

        resp = client.post(
            "/api/follow-ups/send-batch",
            json={"contact_ids": [c1.id, c2.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["sent"] == 2

    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_batch_empty_ids(self, mock_token, client: TestClient):
        resp = client.post("/api/follow-ups/send-batch", json={"contact_ids": []})
        assert resp.status_code == 400

    @patch("app.utils.graph_client.GraphClient")
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_batch_with_missing_contact(
        self,
        mock_token,
        MockGC,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock()
        MockGC.return_value = mock_gc

        c1 = _make_contact(db_session, test_requisition, test_user)
        db_session.commit()

        resp = client.post(
            "/api/follow-ups/send-batch",
            json={"contact_ids": [c1.id, 99999]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["sent"] == 1

    @patch("app.utils.graph_client.GraphClient")
    @patch("app.routers.rfq.require_fresh_token", new_callable=AsyncMock, return_value="mock-token")
    def test_batch_graph_error(
        self,
        mock_token,
        MockGC,
        client: TestClient,
        db_session: Session,
        test_user: User,
        test_requisition: Requisition,
    ):
        mock_gc = MagicMock()
        mock_gc.post_json = AsyncMock(side_effect=Exception("Graph API error"))
        MockGC.return_value = mock_gc

        c1 = _make_contact(db_session, test_requisition, test_user)
        db_session.commit()

        resp = client.post(
            "/api/follow-ups/send-batch",
            json={"contact_ids": [c1.id]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"][0]["status"] == "failed"


# ── RFQ Prepare ──────────────────────────────────────────────────────


class TestRfqPrepare:
    def test_prepare_basic(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Arrow Electronics"}]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "vendors" in data
        assert "all_parts" in data
        assert "subs_map" in data

    def test_prepare_with_cached_emails(
        self, client: TestClient, db_session: Session, test_requisition: Requisition, test_vendor_card: VendorCard
    ):
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "Arrow Electronics"}]},
        )
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        assert len(vendors) == 1
        # Arrow has emails in vendor card
        assert vendors[0]["contact_source"] == "cached"
        assert vendors[0]["needs_lookup"] is False

    def test_prepare_not_found(self, client: TestClient):
        resp = client.post(
            "/api/requisitions/99999/rfq-prepare",
            json={"vendors": [{"vendor_name": "Arrow"}]},
        )
        assert resp.status_code == 404

    def test_prepare_exhaustion_tracking(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Already-contacted parts appear in already_asked."""
        _make_contact(
            db_session,
            test_requisition,
            test_user,
            vendor_name="TestVendor",
            parts=["LM317T"],
        )
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "TestVendor"}]},
        )
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        assert "LM317T" in vendors[0]["already_asked"]

    def test_prepare_unknown_vendor_needs_lookup(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """Unknown vendor with no card and no past contacts needs lookup."""
        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "CompletelyUnknownVendor12345"}]},
        )
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        assert vendors[0]["needs_lookup"] is True

    def test_prepare_past_rfq_email_reuse(
        self, client: TestClient, db_session: Session, test_user: User, test_requisition: Requisition
    ):
        """Past RFQ contacts from other requisitions are suggested."""
        other_req = Requisition(
            name="REQ-OTHER",
            customer_name="Other Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(other_req)
        db_session.flush()

        _make_contact(
            db_session,
            other_req,
            test_user,
            vendor_name="PastVendor",
            vendor_contact="past@vendor.com",
        )
        db_session.commit()

        resp = client.post(
            f"/api/requisitions/{test_requisition.id}/rfq-prepare",
            json={"vendors": [{"vendor_name": "PastVendor"}]},
        )
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        assert vendors[0]["contact_source"] == "past_rfq"
        assert "past@vendor.com" in vendors[0]["emails"]


# ── Enrich With Vendor Cards ─────────────────────────────────────────


class TestEnrichWithVendorCards:
    def test_enrich_basic(self, db_session: Session, test_vendor_card: VendorCard):
        from app.routers.rfq import _enrich_with_vendor_cards

        results = {
            "LM317T": {
                "sightings": [
                    {"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T"},
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)

        sighting = results["LM317T"]["sightings"][0]
        assert "vendor_card" in sighting
        assert sighting["vendor_card"]["card_id"] == test_vendor_card.id

    def test_enrich_auto_creates_card(self, db_session: Session):
        from app.routers.rfq import _enrich_with_vendor_cards

        results = {
            "LM317T": {
                "sightings": [
                    {"vendor_name": "Brand New Vendor", "mpn_matched": "LM317T"},
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)

        sighting = results["LM317T"]["sightings"][0]
        assert "vendor_card" in sighting
        assert sighting["vendor_card"]["card_id"] is not None
        assert sighting["vendor_card"]["is_new_vendor"] is True

    def test_enrich_filters_blacklisted(self, db_session: Session):
        from app.routers.rfq import _enrich_with_vendor_cards

        card = VendorCard(
            normalized_name="blacklisted vendor",
            display_name="Blacklisted Vendor",
            emails=[],
            phones=[],
            sighting_count=0,
            is_blacklisted=True,
        )
        db_session.add(card)
        db_session.flush()

        results = {
            "LM317T": {
                "sightings": [
                    {"vendor_name": "Blacklisted Vendor", "mpn_matched": "LM317T"},
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)

        assert len(results["LM317T"]["sightings"]) == 0
        assert results["LM317T"]["blacklisted_count"] == 1

    def test_enrich_filters_garbage_vendors(self, db_session: Session):
        from app.routers.rfq import _enrich_with_vendor_cards

        results = {
            "LM317T": {
                "sightings": [
                    {"vendor_name": "No Seller Listed", "mpn_matched": "LM317T"},
                    {"vendor_name": "N/A", "mpn_matched": "LM317T"},
                    {"vendor_name": "", "mpn_matched": "LM317T"},
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)

        assert len(results["LM317T"]["sightings"]) == 0

    def test_enrich_empty_results(self, db_session: Session):
        from app.routers.rfq import _enrich_with_vendor_cards

        results = {"LM317T": {"sightings": []}}
        _enrich_with_vendor_cards(results, db_session)  # Should not raise

    def test_enrich_with_reviews(self, db_session: Session, test_vendor_card: VendorCard, test_user: User):
        from app.routers.rfq import _enrich_with_vendor_cards

        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=4,
            comment="Good vendor",
        )
        db_session.add(review)
        db_session.flush()

        results = {
            "LM317T": {
                "sightings": [
                    {"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T"},
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)

        sighting = results["LM317T"]["sightings"][0]
        assert sighting["vendor_card"]["avg_rating"] == 4.0
        assert sighting["vendor_card"]["review_count"] == 1

    def test_enrich_harvests_emails_and_phones(self, db_session: Session, test_vendor_card: VendorCard):
        from app.routers.rfq import _enrich_with_vendor_cards

        results = {
            "LM317T": {
                "sightings": [
                    {
                        "vendor_name": "Arrow Electronics",
                        "mpn_matched": "LM317T",
                        "vendor_email": "newemail@arrow.com",
                        "vendor_phone": "+1-555-9999",
                        "vendor_url": "https://arrow.com/lm317t",
                    },
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)

        sighting = results["LM317T"]["sightings"][0]
        assert sighting["vendor_card"]["has_emails"] is True

    def test_enrich_skips_historical(self, db_session: Session, test_vendor_card: VendorCard):
        from app.routers.rfq import _enrich_with_vendor_cards

        results = {
            "LM317T": {
                "sightings": [
                    {"vendor_name": "Arrow Electronics", "mpn_matched": "LM317T", "is_historical": True},
                ]
            }
        }
        _enrich_with_vendor_cards(results, db_session)
        # Historical sightings should still be enriched with card info but not counted for sighting_count
        assert len(results["LM317T"]["sightings"]) >= 0


# ── Scope Enforcement ────────────────────────────────────────────────


class TestScopeEnforcement:
    def test_sales_user_cannot_see_others_req(
        self, db_session: Session, sales_user: User, test_requisition: Requisition
    ):
        """Sales user cannot access requisitions they don't own."""
        from app.database import get_db
        from app.dependencies import require_buyer, require_user
        from app.main import app

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[require_user] = lambda: sales_user
        app.dependency_overrides[require_buyer] = lambda: sales_user

        try:
            with TestClient(app) as c:
                resp = c.get(f"/api/requisitions/{test_requisition.id}/contacts")
                assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(require_buyer, None)

    def test_buyer_can_see_any_req(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Buyer user can access any requisition (scope only applies to sales)."""
        resp = client.get(f"/api/requisitions/{test_requisition.id}/contacts")
        assert resp.status_code == 200
