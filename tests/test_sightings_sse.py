"""Tests for SSE event publishing from sightings router mutation endpoints.

Called by: pytest
Depends on: conftest.py fixtures, app.services.sse_broker, app.routers.sightings
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.models.sourcing import Requirement, Requisition, Sighting
from app.models.vendor_sighting_summary import VendorSightingSummary


def _seed_data(db_session):
    """Create requisition + requirement + sighting summary for testing."""
    req = Requisition(name="Test RFQ", status="open", customer_name="Acme Corp")
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


def _seed_sighting(db_session, requirement_id):
    """Create an actual sighting to mark unavailable."""
    s = Sighting(
        requirement_id=requirement_id,
        vendor_name="SSE Vendor",
        mpn_matched="SSE-TEST-001",
    )
    db_session.add(s)
    db_session.commit()


class TestSSEPublishOnMutation:
    """Each sightings mutation endpoint publishes a sighting-updated SSE event carrying
    the requirement_id."""

    @pytest.mark.parametrize(
        ("method", "path_suffix", "data", "extra_setup", "asserts_event_name"),
        [
            ("patch", "advance-status", {"status": "sourcing"}, None, True),
            ("post", "log-activity", {"notes": "Test note", "channel": "note", "vendor_name": ""}, None, True),
            (
                "post",
                "mark-unavailable",
                {"vendor_name": "SSE Vendor", "reason": "sold_elsewhere"},
                _seed_sighting,
                False,
            ),
            ("post", "refresh", None, None, False),
        ],
        ids=["advance_status", "log_activity", "mark_unavailable", "refresh"],
    )
    def test_publishes_sighting_updated(
        self, client, db_session, method, path_suffix, data, extra_setup, asserts_event_name
    ):
        _, r, _ = _seed_data(db_session)
        if extra_setup is not None:
            extra_setup(db_session, r.id)

        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            request = getattr(client, method)
            url = f"/v2/partials/sightings/{r.id}/{path_suffix}"
            resp = request(url, data=data) if data is not None else request(url)

            assert resp.status_code == 200
            mock_broker.publish.assert_called_once()
            call_args = mock_broker.publish.call_args
            if asserts_event_name:
                assert call_args[0][1] == "sighting-updated"
            data_published = json.loads(call_args[0][2])
            assert data_published["requirement_id"] == r.id


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
