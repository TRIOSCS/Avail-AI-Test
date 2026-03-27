"""Tests for SSE event publishing from sightings router mutation endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app.services.sse_broker, app.routers.sightings
"""

import json
from unittest.mock import AsyncMock, patch

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary


def _seed_data(db_session):
    """Create requisition + requirement + sighting summary for testing."""
    req = Requisition(name="Test RFQ", status="active", customer_name="Acme Corp")
    db_session.add(req)
    db_session.flush()
    r = Requirement(
        requisition_id=req.id,
        primary_mpn="SSE-TEST-001",
        manufacturer="TestMfr",
        target_qty=100,
        sourcing_status="open",
    )
    db_session.add(r)
    db_session.flush()
    s = VendorSightingSummary(
        requirement_id=r.id,
        vendor_name="SSE Vendor",
        estimated_qty=200,
        listing_count=2,
        score=75.0,
        tier="Good",
    )
    db_session.add(s)
    db_session.commit()
    return req, r, s


class TestSSEPublishOnAdvanceStatus:
    """Verify advance-status endpoint publishes sighting-updated SSE event."""

    def test_publishes_on_status_change(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.patch(
                f"/v2/partials/sightings/{r.id}/advance-status",
                data={"status": "sourcing"},
            )
            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            call_args = mock_broker.publish.call_args
            assert call_args[0][1] == "sighting-updated"
            data = json.loads(call_args[0][2])
            assert data["requirement_id"] == r.id


class TestSSEPublishOnLogActivity:
    """Verify log-activity endpoint publishes sighting-updated SSE event."""

    def test_publishes_on_activity_log(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.post(
                f"/v2/partials/sightings/{r.id}/log-activity",
                data={"notes": "Test note", "channel": "note", "vendor_name": ""},
            )
            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            call_args = mock_broker.publish.call_args
            assert call_args[0][1] == "sighting-updated"
            data = json.loads(call_args[0][2])
            assert data["requirement_id"] == r.id


class TestSSEPublishOnMarkUnavailable:
    """Verify mark-unavailable endpoint publishes sighting-updated SSE event."""

    def test_publishes_on_mark_unavailable(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        # Create an actual sighting to mark
        s = Sighting(
            requirement_id=r.id,
            vendor_name="SSE Vendor",
            mpn_matched="SSE-TEST-001",
        )
        db_session.add(s)
        db_session.commit()

        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.post(
                f"/v2/partials/sightings/{r.id}/mark-unavailable",
                data={"vendor_name": "SSE Vendor"},
            )
            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            data = json.loads(mock_broker.publish.call_args[0][2])
            assert data["requirement_id"] == r.id


class TestSSEPublishOnAssignBuyer:
    """Verify assign-buyer endpoint publishes sighting-updated SSE event."""

    def test_publishes_on_assign(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.patch(
                f"/v2/partials/sightings/{r.id}/assign",
                data={"assigned_buyer_id": "1"},
            )
            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            data = json.loads(mock_broker.publish.call_args[0][2])
            assert data["requirement_id"] == r.id


class TestSSEPublishOnRefresh:
    """Verify single refresh endpoint publishes sighting-updated SSE event."""

    def test_publishes_on_refresh(self, client, db_session):
        _, r, _ = _seed_data(db_session)
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.post(f"/v2/partials/sightings/{r.id}/refresh")
            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            data = json.loads(mock_broker.publish.call_args[0][2])
            assert data["requirement_id"] == r.id


class TestSSEPublishOnBatchRefresh:
    """Verify batch-refresh endpoint publishes sighting-updated for each requirement."""

    def test_publishes_for_each_requirement(self, client, db_session):
        _, r1, _ = _seed_data(db_session)
        # Create second requirement
        r2 = Requirement(
            requisition_id=r1.requisition_id,
            primary_mpn="SSE-TEST-002",
            manufacturer="TestMfr",
            target_qty=50,
            sourcing_status="open",
        )
        db_session.add(r2)
        db_session.commit()

        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([r1.id, r2.id])},
            )
            assert resp.status_code == 200
            # Should publish for each requirement in the batch
            assert mock_broker.publish.call_count == 2
            published_ids = {json.loads(call[0][2])["requirement_id"] for call in mock_broker.publish.call_args_list}
            assert published_ids == {r1.id, r2.id}


class TestSSEPublishChannelFormat:
    """Verify SSE events are published to the correct user channel."""

    def test_channel_includes_user_id(self, client, db_session, test_user):
        _, r, _ = _seed_data(db_session)
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            client.patch(
                f"/v2/partials/sightings/{r.id}/assign",
                data={"assigned_buyer_id": "1"},
            )
            channel = mock_broker.publish.call_args[0][0]
            assert channel == f"user:{test_user.id}"
