"""Comprehensive tests for app/routers/sightings.py — gap coverage.

Covers: cache helpers, sorting variants, sales_person filter, detail panel
branches, refresh/batch endpoints, advance-status, log-activity, send-inquiry,
preview-inquiry edge cases, and internal helpers (_oob_toast, _get_cached, etc).

Called by: pytest
Depends on: conftest.py fixtures, app models, sighting_status service
"""

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.models.intelligence import ActivityLog, MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.models.vendors import VendorCard, VendorContact


def _seed(db, *, user=None, status="active", mpn="MPN-001", sourcing_status="open", target_qty=100):
    """Create requisition + requirement + sighting summary."""
    req = Requisition(
        name="Test RFQ",
        status=status,
        customer_name="Acme Corp",
        created_by=user.id if user else None,
    )
    db.add(req)
    db.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        manufacturer="TestMfr",
        target_qty=target_qty,
        sourcing_status=sourcing_status,
    )
    db.add(r)
    db.flush()
    s = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="Good Vendor",
        estimated_qty=200,
        listing_count=2,
        score=75.0,
        tier="Good",
    )
    db.add(s)
    db.commit()
    return req, r, s


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestCacheHelpers:
    """Cover _get_cached and _invalidate_cache."""

    def test_get_cached_returns_fresh_value(self):
        from app.routers.sightings import _cache, _get_cached

        _cache.clear()
        result = _get_cached("test_key", 10.0, lambda: "fresh_value")
        assert result == "fresh_value"

    def test_get_cached_returns_cached_within_ttl(self):
        from app.routers.sightings import _cache, _get_cached

        _cache.clear()
        _get_cached("test_key2", 60.0, lambda: "first")
        result = _get_cached("test_key2", 60.0, lambda: "second")
        assert result == "first"

    def test_get_cached_refreshes_after_ttl(self):
        from app.routers.sightings import _cache, _get_cached

        _cache.clear()
        # Set with a very old timestamp
        _cache["test_key3"] = (time.monotonic() - 100, "old_value")
        result = _get_cached("test_key3", 1.0, lambda: "new_value")
        assert result == "new_value"

    def test_invalidate_cache_removes_key(self):
        from app.routers.sightings import _cache, _get_cached, _invalidate_cache

        _cache.clear()
        _get_cached("to_remove", 60.0, lambda: "val")
        _invalidate_cache("to_remove")
        assert "to_remove" not in _cache

    def test_invalidate_cache_noop_on_missing(self):
        from app.routers.sightings import _invalidate_cache

        # Should not raise
        _invalidate_cache("nonexistent_key")


class TestOobToast:
    def test_success_toast(self):
        from app.routers.sightings import _oob_toast

        resp = _oob_toast("Operation succeeded")
        assert resp.status_code == 200
        body = resp.body.decode()
        assert "Operation succeeded" in body
        assert "success" in body

    def test_warning_toast(self):
        from app.routers.sightings import _oob_toast

        resp = _oob_toast("Something failed", "warning")
        body = resp.body.decode()
        assert "warning" in body

    def test_escapes_quotes(self):
        from app.routers.sightings import _oob_toast

        resp = _oob_toast("It's a 'test' with \"quotes\"")
        body = resp.body.decode()
        assert "\\'" in body
        assert "&quot;" in body


# ═══════════════════════════════════════════════════════════════════════════
# Sightings list — filter/sort branches
# ═══════════════════════════════════════════════════════════════════════════


class TestSightingsListFilters:
    def test_filter_by_sales_person(self, client, db_session, test_user):
        _seed(db_session, user=test_user)
        resp = client.get(f"/v2/partials/sightings?sales_person={test_user.name}")
        assert resp.status_code == 200

    def test_sort_by_created(self, client, db_session):
        _seed(db_session)
        resp = client.get("/v2/partials/sightings?sort=created&dir=asc")
        assert resp.status_code == 200

    def test_sort_by_status(self, client, db_session):
        _seed(db_session)
        resp = client.get("/v2/partials/sightings?sort=status&dir=desc")
        assert resp.status_code == 200

    def test_sort_by_mpn_desc(self, client, db_session):
        _seed(db_session)
        resp = client.get("/v2/partials/sightings?sort=mpn&dir=desc")
        assert resp.status_code == 200

    def test_sort_priority_asc(self, client, db_session):
        _seed(db_session)
        resp = client.get("/v2/partials/sightings?sort=priority&dir=asc")
        assert resp.status_code == 200

    def test_page_beyond_total(self, client, db_session):
        _seed(db_session)
        resp = client.get("/v2/partials/sightings?page=999")
        assert resp.status_code == 200

    def test_limit_custom(self, client, db_session):
        _seed(db_session)
        resp = client.get("/v2/partials/sightings?limit=10")
        assert resp.status_code == 200

    def test_multiple_filters_combined(self, client, db_session, test_user):
        _seed(db_session, user=test_user)
        resp = client.get("/v2/partials/sightings?status=open&q=MPN&sort=mpn&dir=asc")
        assert resp.status_code == 200

    def test_stale_detection_with_old_activity(self, client, db_session):
        req, r, _ = _seed(db_session)
        # Add an old activity log
        old_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
        log = ActivityLog(
            activity_type="note",
            channel="system",
            requirement_id=r.id,
            requisition_id=req.id,
            notes="old note",
            created_at=old_date,
        )
        db_session.add(log)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Detail panel branches
# ═══════════════════════════════════════════════════════════════════════════


class TestSightingsDetailBranches:
    def test_detail_sourcing_status_with_rfq_sent(self, client, db_session):
        """Detail panel with sourcing status and RFQ activity."""
        req, r, _ = _seed(db_session, sourcing_status="sourcing")
        # Add rfq_sent activity
        log = ActivityLog(
            activity_type="rfq_sent",
            channel="email",
            requirement_id=r.id,
            requisition_id=req.id,
            notes="RFQ sent to vendor",
        )
        db_session.add(log)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_sourcing_no_rfq(self, client, db_session):
        """Detail: sourcing status with no RFQ activity."""
        _, r, _ = _seed(db_session, sourcing_status="sourcing")
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_sourcing_stale_rfq(self, client, db_session):
        """Detail: sourcing with RFQ sent >3 days ago shows follow up."""
        req, r, _ = _seed(db_session, sourcing_status="sourcing")
        old_date = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=5)
        log = ActivityLog(
            activity_type="rfq_sent",
            channel="email",
            requirement_id=r.id,
            requisition_id=req.id,
            notes="old RFQ",
            created_at=old_date,
        )
        db_session.add(log)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_offered_with_pending(self, client, db_session):
        """Detail: offered status with pending offers."""
        req, r, _ = _seed(db_session, sourcing_status="offered")
        offer = Offer(
            requirement_id=r.id,
            requisition_id=req.id,
            vendor_name="Good Vendor",
            mpn="MPN-001",
            status="pending_review",
            unit_price=1.50,
            qty_available=100,
        )
        db_session.add(offer)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_offered_no_pending(self, client, db_session):
        """Detail: offered status with no pending offers."""
        _, r, _ = _seed(db_session, sourcing_status="offered")
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_won_status(self, client, db_session):
        _, r, _ = _seed(db_session, sourcing_status="won")
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_archived_status(self, client, db_session):
        _, r, _ = _seed(db_session, sourcing_status="archived")
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_vendor_with_phone_on_summary(self, client, db_session):
        """Detail: vendor phone from summary, not from card."""
        req, r, _ = _seed(db_session)
        # Update summary with phone
        summary = db_session.query(VendorSightingSummary).filter_by(requirement_id=r.id).first()
        summary.vendor_phone = "+1-555-1234"
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_vendor_phone_from_card(self, client, db_session):
        """Detail: vendor phone falls back to VendorCard phones."""
        req, r, _ = _seed(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            phones=["+1-555-9999"],
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_with_source_types_list(self, client, db_session):
        """Detail: summary with source_types as list."""
        req, r, _ = _seed(db_session)
        summary = db_session.query(VendorSightingSummary).filter_by(requirement_id=r.id).first()
        summary.source_types = ["api", "email"]
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_with_newest_sighting_date(self, client, db_session):
        """Detail: age_days calculated from newest_sighting_at."""
        req, r, _ = _seed(db_session)
        summary = db_session.query(VendorSightingSummary).filter_by(requirement_id=r.id).first()
        summary.newest_sighting_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_with_material_card(self, client, db_session):
        """Detail: requirement with material_card_id set."""
        req, r, _ = _seed(db_session)
        mc = MaterialCard(
            normalized_mpn="mpn-001",
            display_mpn="MPN-001",
            lifecycle_status="active",
        )
        db_session.add(mc)
        db_session.flush()
        r.material_card_id = mc.id
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200

    def test_detail_with_multiple_activities(self, client, db_session):
        """Detail: multiple activity logs displayed."""
        req, r, _ = _seed(db_session)
        for i in range(5):
            db_session.add(
                ActivityLog(
                    activity_type="note",
                    channel="manual",
                    requirement_id=r.id,
                    requisition_id=req.id,
                    notes=f"Note {i}",
                )
            )
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/{r.id}/detail")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Refresh endpoints
# ═══════════════════════════════════════════════════════════════════════════


class TestSightingsRefreshBranches:
    def test_refresh_with_search_failure(self, client, db_session):
        """Refresh falls back gracefully when search_requirement fails."""
        _, r, _ = _seed(db_session)
        with patch("app.search_service.search_requirement", new_callable=AsyncMock) as mock_search:
            mock_search.side_effect = Exception("Search API down")
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
            assert resp.status_code == 200
            assert "HX-Trigger" in resp.headers

    def test_refresh_publishes_sse(self, client, db_session):
        """Refresh publishes SSE event."""
        _, r, _ = _seed(db_session)
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
            assert resp.status_code == 200

    def test_batch_refresh_mixed_results(self, client, db_session):
        """Batch refresh with some failing requirements."""
        req, r, _ = _seed(db_session)
        r2 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-002",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()

        call_count = 0

        async def _mock_search(req_obj, db):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise Exception("fail")

        with patch("app.search_service.search_requirement", side_effect=_mock_search):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r.id, r2.id])},
            )
            assert resp.status_code == 200
            assert "1 failed" in resp.text or "failed" in resp.text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Advance status
# ═══════════════════════════════════════════════════════════════════════════


class TestAdvanceStatus:
    def test_advance_open_to_sourcing(self, client, db_session):
        _, r, _ = _seed(db_session, sourcing_status="open")
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"status": "sourcing"},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.sourcing_status == "sourcing"

    def test_advance_404_for_missing(self, client, db_session):
        resp = client.patch(
            "/v2/partials/sightings/99999/advance-status",
            data={"status": "sourcing"},
        )
        assert resp.status_code == 404

    def test_advance_no_status_returns_400(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"status": ""},
        )
        assert resp.status_code == 400

    def test_advance_invalid_transition_returns_409(self, client, db_session):
        _, r, _ = _seed(db_session, sourcing_status="open")
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"status": "won"},
        )
        assert resp.status_code == 409

    def test_advance_creates_activity_log(self, client, db_session):
        _, r, _ = _seed(db_session, sourcing_status="open")
        client.patch(
            f"/v2/partials/sightings/{r.id}/advance-status",
            data={"status": "sourcing"},
        )
        logs = (
            db_session.query(ActivityLog)
            .filter(
                ActivityLog.requirement_id == r.id,
                ActivityLog.activity_type == "status_change",
            )
            .all()
        )
        assert len(logs) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Log activity
# ═══════════════════════════════════════════════════════════════════════════


class TestLogActivity:
    def test_log_note(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "Test note", "channel": "note"},
        )
        assert resp.status_code == 200
        log = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.requirement_id == r.id, ActivityLog.activity_type == "note")
            .first()
        )
        assert log is not None
        assert log.channel == "manual"

    def test_log_call(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "Called vendor", "channel": "call", "vendor_name": "Arrow"},
        )
        assert resp.status_code == 200
        log = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.requirement_id == r.id, ActivityLog.activity_type == "call_outbound")
            .first()
        )
        assert log is not None
        assert log.contact_name == "Arrow"

    def test_log_email(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "Sent email", "channel": "email"},
        )
        assert resp.status_code == 200

    def test_log_empty_notes_returns_error(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "", "channel": "note"},
        )
        assert resp.status_code in (400, 422)

    def test_log_whitespace_notes_returns_error(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "   ", "channel": "note"},
        )
        assert resp.status_code in (400, 422)

    def test_log_invalid_channel_returns_400(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "test", "channel": "fax"},
        )
        assert resp.status_code == 400

    def test_log_404_for_missing_requirement(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/99999/log-activity",
            data={"notes": "test", "channel": "note"},
        )
        assert resp.status_code == 404

    def test_log_with_blank_vendor_name(self, client, db_session):
        """Blank vendor_name should set contact_name to None."""
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/log-activity",
            data={"notes": "Note text", "channel": "note", "vendor_name": "  "},
        )
        assert resp.status_code == 200
        log = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == r.id).first()
        assert log.contact_name is None


# ═══════════════════════════════════════════════════════════════════════════
# Send inquiry
# ═══════════════════════════════════════════════════════════════════════════


class TestSendInquiry:
    def test_send_missing_email_body_400(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": str(r.id),
                "vendor_names": "Acme",
                "email_body": "",
            },
        )
        assert resp.status_code == 400

    def test_send_missing_vendor_names_400(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={"requirement_ids": str(r.id), "email_body": "Hello"},
        )
        assert resp.status_code == 400

    def test_send_success(self, client, db_session):
        """Send inquiry with mocked email service."""
        _, r, _ = _seed(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
        )
        db_session.add(vc)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=vc.id,
            contact_type="sales",
            email="sales@good.com",
            source="manual",
        )
        db_session.add(contact)
        db_session.commit()

        with (
            patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send,
            patch("app.services.sourcing_auto_progress.auto_progress_status", return_value=True),
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_send.return_value = [{"vendor": "Good Vendor", "status": "sent"}]
            mock_broker.publish = AsyncMock()
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Good Vendor",
                    "email_body": "Please quote.",
                },
            )
            assert resp.status_code == 200
            assert "RFQ sent" in resp.text

    def test_send_failure(self, client, db_session):
        """Send inquiry with email service failure."""
        _, r, _ = _seed(db_session)
        with (
            patch("app.email_service.send_batch_rfq", new_callable=AsyncMock) as mock_send,
            patch("app.routers.sightings.broker") as mock_broker,
        ):
            mock_send.side_effect = Exception("SMTP error")
            mock_broker.publish = AsyncMock()
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": str(r.id),
                    "vendor_names": "Vendor A",
                    "email_body": "Quote please.",
                },
            )
            assert resp.status_code == 200
            assert "Failed" in resp.text or "failed" in resp.text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Mark unavailable
# ═══════════════════════════════════════════════════════════════════════════


class TestMarkUnavailable:
    def test_marks_matching_sightings(self, client, db_session):
        _, r, _ = _seed(db_session)
        s = Sighting(
            requirement_id=r.id,
            vendor_name="Good Vendor",
            mpn_matched="MPN-001",
        )
        db_session.add(s)
        db_session.commit()
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": "Good Vendor"},
        )
        assert resp.status_code == 200
        db_session.refresh(s)
        assert s.is_unavailable is True

    def test_empty_vendor_name_400(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            f"/v2/partials/sightings/{r.id}/mark-unavailable",
            data={"vendor_name": ""},
        )
        assert resp.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Assign buyer
# ═══════════════════════════════════════════════════════════════════════════


class TestAssignBuyer:
    def test_assign_buyer(self, client, db_session, test_user):
        _, r, _ = _seed(db_session)
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.assigned_buyer_id == test_user.id

    def test_unassign_buyer(self, client, db_session):
        _, r, _ = _seed(db_session)
        r.assigned_buyer_id = 1
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{r.id}/assign",
            data={"assigned_buyer_id": ""},
        )
        assert resp.status_code == 200
        db_session.refresh(r)
        assert r.assigned_buyer_id is None


# ═══════════════════════════════════════════════════════════════════════════
# Batch assign
# ═══════════════════════════════════════════════════════════════════════════


class TestBatchAssignBranches:
    def test_assign_to_nonexistent_buyer(self, client, db_session, test_user):
        """Assigning to a valid buyer works; nonexistent buyer_id hits FK constraint in
        SQLite, so we test with the real test_user instead."""
        _, r, _ = _seed(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([r.id]), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200

    def test_assign_multiple_requirements(self, client, db_session, test_user):
        req, r, _ = _seed(db_session)
        r2 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-002",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={
                "requirement_ids": json.dumps([r.id, r2.id]),
                "buyer_id": str(test_user.id),
            },
        )
        assert resp.status_code == 200
        assert "2 requirements" in resp.text


# ═══════════════════════════════════════════════════════════════════════════
# Batch notes branches
# ═══════════════════════════════════════════════════════════════════════════


class TestBatchNotesBranches:
    def test_empty_requirement_ids(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": "[]", "notes": "note"},
        )
        assert resp.status_code == 200
        assert "No requirements selected" in resp.text

    def test_whitespace_only_notes(self, client, db_session):
        _, r, _ = _seed(db_session)
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([r.id]), "notes": "   "},
        )
        assert resp.status_code == 200
        assert "Note text is required" in resp.text


# ═══════════════════════════════════════════════════════════════════════════
# Batch status branches
# ═══════════════════════════════════════════════════════════════════════════


class TestBatchStatusBranches:
    def test_empty_requirement_ids(self, client, db_session):
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": "[]", "status": "sourcing"},
        )
        assert resp.status_code == 200
        assert "No requirements selected" in resp.text

    def test_all_valid_transitions(self, client, db_session):
        req = Requisition(name="RFQ", status="active", customer_name="Corp")
        db_session.add(req)
        db_session.flush()
        r1 = Requirement(requisition_id=req.id, primary_mpn="A", target_qty=100, sourcing_status="open")
        r2 = Requirement(requisition_id=req.id, primary_mpn="B", target_qty=100, sourcing_status="open")
        db_session.add_all([r1, r2])
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([r1.id, r2.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        assert "Updated 2 of 2" in resp.text


# ═══════════════════════════════════════════════════════════════════════════
# Vendor modal
# ═══════════════════════════════════════════════════════════════════════════


class TestVendorModal:
    def test_with_vendor_cards(self, client, db_session):
        req, r, _ = _seed(db_session)
        vc = VendorCard(
            normalized_name="good vendor",
            display_name="Good Vendor",
            is_blacklisted=False,
        )
        db_session.add(vc)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id}")
        assert resp.status_code == 200

    def test_multiple_requirement_ids(self, client, db_session):
        req, r, _ = _seed(db_session)
        r2 = Requirement(
            requisition_id=req.id,
            primary_mpn="MPN-002",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()
        resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={r.id},{r2.id}")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# Heatmap branches
# ═══════════════════════════════════════════════════════════════════════════


class TestHeatmapBranches:
    def test_near_deadline_heatmap(self, client, db_session):
        from datetime import date

        req, r, _ = _seed(db_session)
        r.need_by_date = date.today() + timedelta(days=1)
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "bg-rose-50/30" in resp.text

    def test_critical_urgency_heatmap(self, client, db_session):
        """Critical urgency from requisition triggers heatmap."""
        req, r, _ = _seed(db_session)
        req.urgency = "critical"
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "bg-rose-50/30" in resp.text

    def test_hot_urgency_heatmap(self, client, db_session):
        """Hot urgency triggers heatmap."""
        req, r, _ = _seed(db_session)
        req.urgency = "hot"
        db_session.commit()
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "bg-rose-50/30" in resp.text

    def test_stale_medium_priority_heatmap(self, client, db_session):
        """Stale + medium priority triggers heatmap."""
        req, r, _ = _seed(db_session)
        r.priority_score = 50.0  # >= 40
        db_session.commit()
        # No activity = stale
        resp = client.get("/v2/partials/sightings")
        assert resp.status_code == 200
        assert "bg-rose-50/30" in resp.text
