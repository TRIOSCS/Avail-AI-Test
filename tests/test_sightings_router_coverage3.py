"""tests/test_sightings_router_coverage3.py — Coverage boost for app/routers/sightings.py.

Targets uncovered lines:
- batch-refresh: success, failed (id not found), skipped+failed mixed, only-skipped level,
  only-success level, requirement ids that resolve but none found
- batch-assign: empty list, too many, with buyer_id (valid user + unknown user)
- batch-status: empty list, too many, invalid status, updated+skipped mix, all-skipped
- batch-notes: empty list, too many, empty notes, success with activity creation
- mark-unavailable: missing vendor_name
- advance-status: success valid transition, missing status, not found, invalid transition
- preview-inquiry: success with vendor card + contact email
- send-inquiry: success with vendor card, failed send path, auto-progress on multiple
- lines 1079, 1082, 1086, 1090, 1093-1095, 1098-1128 (preview-inquiry body)
- lines 1153, 1156, 1160, 1164, 1167-1174, 1186-1240 (send-inquiry body)

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_user, test_vendor_card,
            test_vendor_contact)
"""

import os

os.environ["TESTING"] = "1"

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, User, VendorCard, VendorContact


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture()
def req_item(db_session: Session, test_user: User):
    """Requisition + Requirement with sourcing_status=open."""
    req = Requisition(
        name="COV3-REQ",
        customer_name="Cov Corp",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="AT89C51",
        target_qty=500,
        sourcing_status="open",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(req)
    db_session.refresh(item)
    return req, item


@pytest.fixture()
def two_items(db_session: Session, test_user: User):
    """Two Requirements under one Requisition."""
    req = Requisition(
        name="COV3-REQ-TWO",
        customer_name="Multi Corp",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    items = []
    for mpn in ("NE555", "LM741"):
        item = Requirement(
            requisition_id=req.id,
            primary_mpn=mpn,
            target_qty=100,
            sourcing_status="open",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        items.append(item)
    db_session.commit()
    for it in items:
        db_session.refresh(it)
    return req, items


# ── batch-refresh ─────────────────────────────────────────────────────────────


class TestBatchRefreshCoverage:
    def test_batch_refresh_success_path(self, client: TestClient, req_item):
        """Lines 651-666, 679-693: success count > 0, level='success'."""
        _, item = req_item
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "1/1" in resp.text

    def test_batch_refresh_id_not_found_increments_failed(self, client: TestClient):
        """Lines 657-659: req_obj is None → failed += 1."""
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([999999])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "failed" in resp.text.lower()

    def test_batch_refresh_only_skipped_level_info(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Lines 689-690: skipped and not success → level='info'."""
        _, item = req_item
        # Set recently searched so it gets rate-limited
        item.last_searched_at = datetime.utcnow() - timedelta(seconds=5)
        db_session.commit()
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower()

    def test_batch_refresh_mixed_failed_and_skipped(
        self, client: TestClient, two_items, db_session: Session
    ):
        """Lines 683-688: failed > 0 → level='warning', skipped text appended."""
        _, items = two_items
        # Rate-limit first item
        items[0].last_searched_at = datetime.utcnow() - timedelta(seconds=5)
        db_session.commit()
        with patch(
            "app.search_service.search_requirement",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([items[0].id, items[1].id])},
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        # Either failed or skipped text must appear
        assert "failed" in resp.text.lower() or "skipped" in resp.text.lower()

    def test_batch_refresh_too_many(self, client: TestClient):
        """Line 642-643: exceeds MAX_BATCH_SIZE."""
        ids = list(range(1, 52))  # 51 > 50
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(ids)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_refresh_non_list_json(self, client: TestClient):
        """Line 637-638: parsed JSON is not a list → treated as empty."""
        with patch("app.search_service.search_requirement", new=AsyncMock()):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps({"key": "value"})},
                headers={"HX-Request": "true"},
            )
        # non-list parsed → requirement_ids = [] → 0 searched
        assert resp.status_code == 200
        assert "0/0" in resp.text


# ── batch-assign ─────────────────────────────────────────────────────────────


class TestBatchAssignCoverage:
    def test_batch_assign_empty_list_returns_warning(self, client: TestClient):
        """Line 712-713: empty requirement_ids → warning toast."""
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": "[]"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No requirements selected" in resp.text

    def test_batch_assign_too_many(self, client: TestClient):
        """Line 709-710: exceeds MAX_BATCH_SIZE."""
        ids = list(range(1, 52))
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps(ids)},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_assign_with_buyer_id_valid_user(
        self, client: TestClient, req_item, test_user: User
    ):
        """Lines 719-721: buyer_id resolves to known user → buyer_name = user.name."""
        _, item = req_item
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={
                "requirement_ids": json.dumps([item.id]),
                "buyer_id": str(test_user.id),
            },
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Assigned" in resp.text

    def test_batch_assign_with_unknown_buyer_id(
        self, client: TestClient, req_item, db_session: Session, test_user: User
    ):
        """Line 721: buyer_id provided but db.get returns None → buyer_name = 'user {id}'.

        Uses a second (non-test) user that is then deleted so db.get returns None,
        while still satisfying the FK constraint at commit time via SET NULL.
        We mock db.get to return None directly to exercise the else branch.
        """
        from unittest.mock import patch as _patch

        _, item = req_item
        original_get = db_session.get

        def _mocked_get(model, pk):
            from app.models import User as _User

            if model is _User and pk == test_user.id:
                return None  # simulate not found
            return original_get(model, pk)

        with _patch.object(db_session, "get", side_effect=_mocked_get):
            resp = client.post(
                "/v2/partials/sightings/batch-assign",
                data={
                    "requirement_ids": json.dumps([item.id]),
                    "buyer_id": str(test_user.id),
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Assigned" in resp.text

    def test_batch_assign_no_buyer_id(self, client: TestClient, req_item):
        """Lines 715-729: no buyer_id → buyer_name stays 'nobody'."""
        _, item = req_item
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([item.id])},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "nobody" in resp.text


# ── batch-status ─────────────────────────────────────────────────────────────


class TestBatchStatusCoverage:
    def test_batch_status_empty_list(self, client: TestClient):
        """Line 750-751: empty requirement_ids → warning toast."""
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": "[]", "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No requirements selected" in resp.text

    def test_batch_status_too_many(self, client: TestClient):
        """Line 747-748: exceeds MAX_BATCH_SIZE."""
        ids = list(range(1, 52))
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps(ids), "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_status_invalid_status_value(self, client: TestClient, req_item):
        """Lines 753-756: SourcingStatus(new_status) raises ValueError → 400."""
        _, item = req_item
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "nonsense_status"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_status_valid_transition_updates(self, client: TestClient, req_item):
        """Lines 759-788: valid open→sourcing transition → updated=1, level=success."""
        _, item = req_item
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Updated 1" in resp.text

    def test_batch_status_skipped_invalid_transition(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Lines 778-787: invalid transition → skipped > 0, level=warning."""
        _, item = req_item
        # won is a terminal state that can't transition to sourcing
        item.sourcing_status = "won"
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower()

    def test_batch_status_two_items_mixed(
        self, client: TestClient, two_items, db_session: Session
    ):
        """Both items: one valid, one invalid transition → updated=1, skipped=1."""
        _, items = two_items
        items[1].sourcing_status = "won"  # can't go to sourcing
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([items[0].id, items[1].id]), "status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower()


# ── batch-notes ──────────────────────────────────────────────────────────────


class TestBatchNotesCoverage:
    def test_batch_notes_empty_list(self, client: TestClient):
        """Line 807-808: empty requirement_ids → warning."""
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": "[]", "notes": "hello"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "No requirements selected" in resp.text

    def test_batch_notes_too_many(self, client: TestClient):
        """Line 804-805: exceeds MAX_BATCH_SIZE."""
        ids = list(range(1, 52))
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps(ids), "notes": "note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_batch_notes_empty_notes_text(self, client: TestClient, req_item):
        """Lines 810-811: notes is empty string → warning."""
        _, item = req_item
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id]), "notes": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Note text is required" in resp.text

    def test_batch_notes_success(self, client: TestClient, req_item):
        """Lines 813-831: notes added to all requirements, success toast."""
        _, item = req_item
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id]), "notes": "follow up needed"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "Added note" in resp.text

    def test_batch_notes_multiple_requirements(self, client: TestClient, two_items):
        """Plural form: 'Added note to 2 requirements'."""
        _, items = two_items
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([it.id for it in items]), "notes": "batch note"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200
        assert "2 requirement" in resp.text


# ── mark-unavailable ──────────────────────────────────────────────────────────


class TestMarkUnavailableCoverage:
    def test_mark_unavailable_missing_vendor_name(self, client: TestClient, req_item):
        """Line 844-845: vendor_name missing → 400."""
        _, item = req_item
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/mark-unavailable",
            data={},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_mark_unavailable_success_with_sightings(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Lines 847-866: sightings found and marked unavailable."""
        from app.models import Sighting

        _, item = req_item
        sighting = Sighting(
            requirement_id=item.id,
            vendor_name="TestVendor",
            mpn_matched="AT89C51",
            qty_available=100,
            unit_price=1.50,
            source_type="brokerbin",
        )
        db_session.add(sighting)
        db_session.commit()

        resp = client.post(
            f"/v2/partials/sightings/{item.id}/mark-unavailable",
            data={"vendor_name": "TestVendor"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200


# ── advance-status ────────────────────────────────────────────────────────────


class TestAdvanceStatusCoverage:
    def test_advance_status_missing_status(self, client: TestClient, req_item):
        """Lines 906-908: empty status → 400."""
        _, item = req_item
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": ""},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 400

    def test_advance_status_not_found(self, client: TestClient):
        """Lines 910-912: requirement not found → 404."""
        resp = client.patch(
            "/v2/partials/sightings/999999/advance-status",
            data={"status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 404

    def test_advance_status_valid_open_to_sourcing(self, client: TestClient, req_item):
        """Lines 910-939: valid open→sourcing transition → updates and returns detail."""
        _, item = req_item
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 200

    def test_advance_status_invalid_transition_returns_409(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Line 917: require_valid_transition raises → 409."""
        _, item = req_item
        item.sourcing_status = "won"  # terminal, can only go to archived
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": "sourcing"},
            headers={"HX-Request": "true"},
        )
        assert resp.status_code == 409


# ── preview-inquiry ───────────────────────────────────────────────────────────


class TestPreviewInquiryCoverage:
    def test_preview_with_vendor_card_no_contact(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Lines 1079-1128: vendor card found, no contact → vendor_email empty."""
        _, item = req_item
        card = VendorCard(
            normalized_name="preview vendor",
            display_name="Preview Vendor",
            emails=[],
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)

        with patch("app.email_service._build_html_body", return_value="<p>body</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": card.display_name,
                    "email_body": "Please quote",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    def test_preview_with_vendor_card_and_contact_email(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Lines 1100-1105: contact has email → vendor_email populated."""
        _, item = req_item
        card = VendorCard(
            normalized_name="contact vendor preview",
            display_name="Contact Vendor Preview",
            emails=[],
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            full_name="Sales Rep",
            email="sales@contactvendorpreview.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.commit()

        with patch("app.email_service._build_html_body", return_value="<p>body</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": card.display_name,
                    "email_body": "Please quote",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    def test_preview_multiple_requirements_subject(
        self, client: TestClient, two_items
    ):
        """Line 1107: plural 'parts' in subject when len > 1."""
        _, items = two_items
        with patch("app.email_service._build_html_body", return_value="<p>body</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": [str(it.id) for it in items],
                    "vendor_names": "Some Vendor",
                    "email_body": "Quote request",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200

    def test_preview_no_requisition_id(self, client: TestClient, db_session: Session, test_user: User):
        """Line 1082: requisition_ids empty → requisition_id=None, avail_token=''."""
        req = Requisition(
            name="ORPHAN-REQ",
            customer_name="Orphan Co",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="ORPHAN1",
            target_qty=10,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        with patch("app.email_service._build_html_body", return_value="<p>body</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": "Any Vendor",
                    "email_body": "Quote please",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200


# ── send-inquiry ──────────────────────────────────────────────────────────────


class TestSendInquiryCoverage:
    def test_send_with_vendor_card_and_contact(
        self, client: TestClient, req_item, db_session: Session
    ):
        """Lines 1153-1174: vendor card + contact email populates vendor_groups."""
        _, item = req_item
        card = VendorCard(
            normalized_name="send vendor",
            display_name="Send Vendor",
            emails=[],
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            full_name="Send Rep",
            email="send@sendvendor.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.commit()

        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{}])):
            with patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=False):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": str(item.id),
                        "vendor_names": card.display_name,
                        "email_body": "Quote needed",
                    },
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert "RFQ sent" in resp.text

    def test_send_with_auto_progress_multiple_reqs(
        self, client: TestClient, two_items
    ):
        """Lines 1215-1217: auto_progress_status returns True for each req → progressed_count."""
        _, items = two_items
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{}])):
            with patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=True):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": [str(it.id) for it in items],
                        "vendor_names": "Some Vendor",
                        "email_body": "Quote needed",
                    },
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert "advanced to sourcing" in resp.text.lower() or "sourcing" in resp.text.lower()

    def test_send_failed_vendors_message(self, client: TestClient, req_item):
        """Lines 1218-1220, 1233-1234: exception → failed_vendors list → warning message."""
        _, item = req_item
        with patch(
            "app.email_service.send_batch_rfq",
            new=AsyncMock(side_effect=RuntimeError("network error")),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(item.id),
                    "vendor_names": "Fail Vendor",
                    "email_body": "Quote please",
                },
                headers={"HX-Request": "true"},
            )
        assert resp.status_code == 200
        assert "Failed" in resp.text or "failed" in resp.text.lower()

    def test_send_single_vendor_singular_message(self, client: TestClient, req_item):
        """Line 1236: sent_count==1 → 'vendor' (not 'vendors') in message."""
        _, item = req_item
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{}])):
            with patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=False):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": str(item.id),
                        "vendor_names": "Single Vendor",
                        "email_body": "Quote please",
                    },
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
        assert "RFQ sent to 1 vendor" in resp.text

    def test_send_no_vendor_card_found(self, client: TestClient, req_item):
        """Lines 1167-1174: vendor_names provided but no card in DB → vendor_email=''."""
        _, item = req_item
        with patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=[{}])):
            with patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=False):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": str(item.id),
                        "vendor_names": "Unknown Vendor XYZ",
                        "email_body": "Quote request",
                    },
                    headers={"HX-Request": "true"},
                )
        assert resp.status_code == 200
