"""test_sightings_batch_ops.py — Tests for sightings batch endpoints.

Covers lines 687-1293: batch-refresh, batch-assign, batch-status, batch-notes,
mark-unavailable, assign-buyer, advance-status, log-activity, vendor-modal,
preview-inquiry, send-inquiry.

Called by: pytest
Depends on: app/routers/sightings.py, tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

# ── Fixtures ─────────────────────────────────────────────────────


def _make_user(db: Session, role: str = "buyer"):
    from app.models import User

    u = User(
        email=f"{role}@test.com",
        name="Test User",
        role=role,
        azure_id=f"{role}-azure-id",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_req_and_requirement(db: Session, user_id: int, mpn: str = "LM317T"):
    from app.models import Requirement, Requisition

    req = Requisition(name="Test Req", status="active", created_by=user_id)
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.upper(),
        target_qty=10,
        sourcing_status="open",
    )
    db.add(requirement)
    db.flush()
    return req, requirement


# ── batch-refresh ─────────────────────────────────────────────────


def test_batch_refresh_empty_list(client, db_session):
    """Batch-refresh with empty requirement_ids returns 200 toast."""
    resp = client.post(
        "/v2/partials/sightings/batch-refresh",
        data={"requirement_ids": "[]"},
    )
    assert resp.status_code == 200


def test_batch_refresh_invalid_json_returns_400(client):
    """Batch-refresh with bad JSON returns 400."""
    resp = client.post(
        "/v2/partials/sightings/batch-refresh",
        data={"requirement_ids": "not-json"},
    )
    assert resp.status_code == 400


def test_batch_refresh_too_many_items(client):
    """Batch-refresh with > MAX_BATCH_SIZE items returns 400."""
    too_many = json.dumps(list(range(1001)))
    resp = client.post(
        "/v2/partials/sightings/batch-refresh",
        data={"requirement_ids": too_many},
    )
    assert resp.status_code == 400


def test_batch_refresh_nonexistent_requirement(client, db_session):
    """Batch-refresh with a non-existent requirement ID counts as failed."""
    with patch("app.search_service.search_requirement", new_callable=AsyncMock):
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "[99999]"},
        )
    assert resp.status_code == 200
    assert "1" in resp.text  # "1 failed" or similar


def test_batch_refresh_valid_requirement(client, db_session, test_user):
    """Batch-refresh calls search_requirement for a valid requirement."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    with patch(
        "app.search_service.search_requirement",
        new_callable=AsyncMock,
        return_value=None,
    ):
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([requirement.id])},
        )

    assert resp.status_code == 200


def test_batch_refresh_runs_searches_in_parallel(client, db_session, test_user):
    """Batch-refresh must run search_requirement calls concurrently.

    With the serial loop, N requirements = N × wall_time. With gather, N requirements ≈
    1 × wall_time. We verify this by giving each search a 0.2s sleep and asserting that
    3 requirements complete in well under 0.6s.
    """
    import asyncio
    import time

    _, req1 = _make_req_and_requirement(db_session, test_user.id)
    _, req2 = _make_req_and_requirement(db_session, test_user.id)
    _, req3 = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    async def slow_search(req_obj, db):
        await asyncio.sleep(0.2)
        return None

    with patch(
        "app.search_service.search_requirement",
        side_effect=slow_search,
    ):
        start = time.perf_counter()
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([req1.id, req2.id, req3.id])},
        )
        elapsed = time.perf_counter() - start

    assert resp.status_code == 200
    # Serial would be ≥0.6s. Parallel should be ~0.2s + overhead.
    # Give generous headroom for CI jitter but stay under the serial floor.
    assert elapsed < 0.5, f"batch-refresh still serial: {elapsed:.3f}s for 3 × 0.2s"


# ── batch-assign ──────────────────────────────────────────────────


def test_batch_assign_sets_buyer(client, db_session, test_user):
    """Batch-assign sets assigned_buyer_id on matched requirements."""

    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "buyer_id": str(test_user.id),
        },
    )

    assert resp.status_code == 200
    db_session.refresh(requirement)
    assert requirement.assigned_buyer_id == test_user.id


def test_batch_assign_empty_list(client):
    """Batch-assign with empty list returns warning toast."""
    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={"requirement_ids": "[]", "buyer_id": "1"},
    )
    assert resp.status_code == 200
    assert "no requirements" in resp.text.lower() or "warning" in resp.text.lower()


def test_batch_assign_too_many(client):
    """Batch-assign with > MAX_BATCH_SIZE returns 400."""
    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={"requirement_ids": json.dumps(list(range(1001))), "buyer_id": "1"},
    )
    assert resp.status_code == 400


# ── batch-status ──────────────────────────────────────────────────


def test_batch_status_empty_list(client):
    """Batch-status with empty list returns warning toast."""
    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={"requirement_ids": "[]", "status": "sourcing"},
    )
    assert resp.status_code == 200
    assert "no requirements" in resp.text.lower() or "warning" in resp.text.lower()


def test_batch_status_invalid_status(client, db_session, test_user):
    """Batch-status with invalid status value returns 400."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "status": "INVALID_STATUS",
        },
    )
    assert resp.status_code == 400


def test_batch_status_valid_transition(client, db_session, test_user):
    """Batch-status updates status for valid transitions."""

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="BC547")
    requirement.sourcing_status = "open"
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "status": "sourcing",
        },
    )

    assert resp.status_code == 200


def test_batch_status_too_many(client):
    """Batch-status with > MAX_BATCH_SIZE returns 400."""
    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={"requirement_ids": json.dumps(list(range(1001))), "status": "sourcing"},
    )
    assert resp.status_code == 400


# ── batch-notes ───────────────────────────────────────────────────


def test_batch_notes_empty_list(client):
    """Batch-notes with empty list returns warning toast."""
    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={"requirement_ids": "[]", "notes": "Test note"},
    )
    assert resp.status_code == 200
    assert "no requirements" in resp.text.lower() or "warning" in resp.text.lower()


def test_batch_notes_empty_notes(client, db_session, test_user):
    """Batch-notes with empty notes returns warning toast."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={"requirement_ids": json.dumps([requirement.id]), "notes": ""},
    )
    assert resp.status_code == 200
    assert "required" in resp.text.lower() or "warning" in resp.text.lower()


def test_batch_notes_creates_activity(client, db_session, test_user):
    """Batch-notes creates ActivityLog entries for each requirement."""
    from app.models import ActivityLog

    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "notes": "Called vendor, waiting for quote",
        },
    )

    assert resp.status_code == 200
    log = db_session.query(ActivityLog).filter_by(requirement_id=requirement.id).first()
    assert log is not None
    assert "called vendor" in log.notes.lower()


def test_batch_notes_too_many(client):
    """Batch-notes with > MAX_BATCH_SIZE returns 400."""
    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={"requirement_ids": json.dumps(list(range(1001))), "notes": "note"},
    )
    assert resp.status_code == 400


# ── mark-unavailable ─────────────────────────────────────────────


def test_mark_unavailable_no_vendor_name(client, db_session, test_user):
    """Mark-unavailable without vendor_name returns 400."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/mark-unavailable",
        data={},
    )
    assert resp.status_code == 400


def test_mark_unavailable_marks_sightings(client, db_session, test_user):
    """Mark-unavailable sets is_unavailable=True for matching vendor sightings."""
    from app.models.sourcing import Sighting

    req_obj, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="NE555")

    sighting = Sighting(
        requirement_id=requirement.id,
        normalized_mpn="NE555",
        vendor_name="Arrow Electronics",
        source_type="manual",
        unit_price=0.50,
        qty_available=1000,
        is_unavailable=False,
    )
    db_session.add(sighting)
    db_session.commit()

    from fastapi.responses import HTMLResponse as _HTMLResponse

    with patch(
        "app.routers.sightings.sightings_detail",
        new_callable=AsyncMock,
        return_value=_HTMLResponse("<div>detail</div>"),
    ):
        resp = client.post(
            f"/v2/partials/sightings/{requirement.id}/mark-unavailable",
            data={"vendor_name": "Arrow Electronics"},
        )

    db_session.refresh(sighting)
    assert sighting.is_unavailable is True


# ── assign-buyer ──────────────────────────────────────────────────


def test_assign_buyer_not_found(client, db_session):
    """Assign-buyer for non-existent requirement returns 404."""
    resp = client.patch(
        "/v2/partials/sightings/99999/assign",
        data={"assigned_buyer_id": "1"},
    )
    assert resp.status_code == 404


def test_assign_buyer_updates_requirement(client, db_session, test_user):
    """Assign-buyer updates assigned_buyer_id on the requirement."""

    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    from fastapi.responses import HTMLResponse as _HTMLResponse

    with patch(
        "app.routers.sightings.sightings_detail",
        new_callable=AsyncMock,
        return_value=_HTMLResponse("<div>ok</div>"),
    ):
        resp = client.patch(
            f"/v2/partials/sightings/{requirement.id}/assign",
            data={"assigned_buyer_id": str(test_user.id)},
        )

    db_session.refresh(requirement)
    assert requirement.assigned_buyer_id == test_user.id


# ── log-activity ──────────────────────────────────────────────────


def test_log_activity_missing_notes(client, db_session, test_user):
    """Log-activity without notes returns 400."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/log-activity",
        data={"notes": "  ", "channel": "note"},
    )
    assert resp.status_code == 400


def test_log_activity_invalid_channel(client, db_session, test_user):
    """Log-activity with invalid channel returns 400."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/log-activity",
        data={"notes": "Test note", "channel": "telegram"},
    )
    assert resp.status_code == 400


def test_log_activity_requirement_not_found(client):
    """Log-activity for missing requirement returns 404."""
    resp = client.post(
        "/v2/partials/sightings/99999/log-activity",
        data={"notes": "Test note", "channel": "note"},
    )
    assert resp.status_code == 404


def test_log_activity_creates_record(client, db_session, test_user):
    """Log-activity creates ActivityLog record."""
    from app.models import ActivityLog

    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.post(
        f"/v2/partials/sightings/{requirement.id}/log-activity",
        data={"notes": "Spoke to vendor", "channel": "call"},
    )

    log = db_session.query(ActivityLog).filter_by(requirement_id=requirement.id).first()
    assert log is not None
    assert log.notes == "Spoke to vendor"
    assert log.activity_type == "call_outbound"


# ── vendor-modal ──────────────────────────────────────────────────


def test_vendor_modal_empty_ids(client):
    """Vendor-modal with empty requirement_ids returns 200 (empty modal)."""
    resp = client.get("/v2/partials/sightings/vendor-modal?requirement_ids=")
    assert resp.status_code == 200


def test_vendor_modal_with_valid_ids(client, db_session, test_user):
    """Vendor-modal with valid IDs returns 200."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id)
    db_session.commit()

    resp = client.get(f"/v2/partials/sightings/vendor-modal?requirement_ids={requirement.id}")
    assert resp.status_code == 200


# ── preview-inquiry ───────────────────────────────────────────────


def test_preview_inquiry_missing_params(client):
    """Preview-inquiry without requirement_ids or vendor_names returns 400."""
    resp = client.post(
        "/v2/partials/sightings/preview-inquiry",
        data={},
    )
    assert resp.status_code == 400


def test_preview_inquiry_returns_preview(client, db_session, test_user):
    """Preview-inquiry returns rendered preview HTML."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="BC547")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/preview-inquiry",
        data={
            "requirement_ids": [str(requirement.id)],
            "vendor_names": ["Arrow Electronics"],
            "email_body": "Please quote the attached parts.",
        },
    )
    assert resp.status_code == 200


# ── send-inquiry ──────────────────────────────────────────────────


def test_send_inquiry_missing_params(client):
    """Send-inquiry without all required fields returns 400."""
    resp = client.post(
        "/v2/partials/sightings/send-inquiry",
        data={"requirement_ids": [], "vendor_names": []},
    )
    assert resp.status_code == 400


def test_send_inquiry_success(client, db_session, test_user):
    """Send-inquiry calls send_batch_rfq and returns success toast."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="STM32F4")
    db_session.commit()

    with patch(
        "app.email_service.send_batch_rfq",
        new_callable=AsyncMock,
        return_value=[{"vendor_name": "Mouser", "sent": True}],
    ):
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": [str(requirement.id)],
                "vendor_names": ["Mouser"],
                "email_body": "Please quote urgently.",
            },
        )

    assert resp.status_code == 200
    assert "sent" in resp.text.lower() or "rfq" in resp.text.lower()


def test_send_inquiry_graph_failure_returns_warning(client, db_session, test_user):
    """Send-inquiry when Graph API fails returns warning toast."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ATMEGA")
    db_session.commit()

    with patch(
        "app.email_service.send_batch_rfq",
        new_callable=AsyncMock,
        side_effect=Exception("Graph API unavailable"),
    ):
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": [str(requirement.id)],
                "vendor_names": ["Arrow"],
                "email_body": "Please quote.",
            },
        )

    assert resp.status_code == 200
    assert "warning" in resp.text.lower() or "failed" in resp.text.lower()
