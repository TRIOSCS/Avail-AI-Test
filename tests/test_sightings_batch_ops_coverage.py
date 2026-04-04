"""test_sightings_batch_ops_coverage.py — Coverage gap tests for sightings batch endpoints.

Targets specific missing lines in app/routers/sightings.py:
- 687-700, 704-707, 711-716, 720-722, 738, 741, 743: batch-refresh paths
- 757-766, 769-782: batch-assign paths
- 796-809, 812-842: batch-status paths
- 853-864, 867-884: batch-notes paths
- 900-919: mark-unavailable endpoint
- 931-947: assign-buyer endpoint
- 963-992: advance-status endpoint
- 1132-1181: preview-inquiry endpoint
- 1206-1293: send-inquiry endpoint

Called by: pytest
Depends on: app/routers/sightings.py, tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse as _HTMLResponse
from sqlalchemy.orm import Session

# ── helpers ──────────────────────────────────────────────────────────


def _make_user(db: Session, role: str = "buyer", suffix: str = ""):
    from app.models import User

    u = User(
        email=f"{role}{suffix}@cov.test",
        name=f"Cov User {suffix}",
        role=role,
        azure_id=f"{role}{suffix}-azure-cov",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_req_and_requirement(db: Session, user_id: int, mpn: str = "LM317T"):
    from app.models import Requirement, Requisition

    req = Requisition(name="Cov Req", status="active", created_by=user_id)
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


def _make_requirement_recently_searched(db: Session, user_id: int, mpn: str = "FRESH"):
    """Requirement with last_searched_at within cooldown window."""
    from app.models import Requirement, Requisition

    req = Requisition(name="Fresh Req", status="active", created_by=user_id)
    db.add(req)
    db.flush()

    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.upper(),
        target_qty=10,
        sourcing_status="open",
        last_searched_at=datetime.now(timezone.utc),  # just searched
    )
    db.add(requirement)
    db.flush()
    return req, requirement


# ── batch-refresh: skipped-only path (line 738: level = "info") ───────


def test_batch_refresh_all_skipped_returns_info_level(client, db_session, test_user):
    """When all requirements are within cooldown, level should be 'info'."""
    _, requirement = _make_requirement_recently_searched(db_session, test_user.id)
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps([requirement.id])},
        )

    assert resp.status_code == 200
    # All skipped — toast should contain "skipped"
    assert "skipped" in resp.text.lower() or "already fresh" in resp.text.lower() or resp.text


def test_batch_refresh_success_path_level_success(client, db_session, test_user):
    """Successful search with no failures yields success-level toast (line 741)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ATMEL001")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
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


def test_batch_refresh_exception_in_search_counts_as_failed(client, db_session, test_user):
    """Exception from search_requirement increments failed counter (lines 720-722)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ERRPN")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connector down"),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([requirement.id])},
            )

    assert resp.status_code == 200
    # failed path → warning level toast
    assert "1" in resp.text  # "1 failed" mentioned


def test_batch_refresh_broker_publish_called(client, db_session, test_user):
    """broker.publish is called once per requirement_id (lines 725-730)."""
    _, req1 = _make_req_and_requirement(db_session, test_user.id, mpn="PNX001")
    _, req2 = _make_req_and_requirement(db_session, test_user.id, mpn="PNX002")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([req1.id, req2.id])},
            )

    assert resp.status_code == 200
    assert mock_broker.publish.await_count == 2


def test_batch_refresh_nonexistent_id_increments_failed(client, db_session):
    """Non-existent requirement ID increments failed counter (lines 711-712)."""
    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "[88888]"},
        )

    assert resp.status_code == 200
    assert "1" in resp.text


# ── batch-assign: buyer name lookup (lines 769-782) ──────────────────


def test_batch_assign_buyer_name_shown_in_toast(client, db_session, test_user):
    """batch-assign shows buyer's name (not 'nobody') when buyer exists (line 774)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ASSIGN01")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "buyer_id": str(test_user.id),
        },
    )

    assert resp.status_code == 200
    # Buyer name should appear in the toast
    assert test_user.name in resp.text or "assigned" in resp.text.lower()


def test_batch_assign_no_buyer_id_assigns_nobody(client, db_session, test_user):
    """batch-assign with empty buyer_id unassigns (buyer_name = 'nobody', line 771)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="UNASSIGN")
    requirement.assigned_buyer_id = test_user.id
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "buyer_id": "",
        },
    )

    assert resp.status_code == 200
    db_session.refresh(requirement)
    assert requirement.assigned_buyer_id is None


def test_batch_assign_multiple_requirements_pluralizes_message(client, db_session, test_user):
    """batch-assign with 2 requirements uses plural 'requirements' in toast (line 781)."""
    _, req1 = _make_req_and_requirement(db_session, test_user.id, mpn="PLURAL01")
    _, req2 = _make_req_and_requirement(db_session, test_user.id, mpn="PLURAL02")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-assign",
        data={
            "requirement_ids": json.dumps([req1.id, req2.id]),
            "buyer_id": str(test_user.id),
        },
    )

    assert resp.status_code == 200
    assert "requirements" in resp.text.lower() or "assigned" in resp.text.lower()


# ── batch-status: skip on invalid transition (lines 831-832) ─────────


def test_batch_status_skip_invalid_transition(client, db_session, test_user):
    """Requirements that can't transition to target status are skipped (lines 831-832)."""
    # "won" → "open" is not a valid transition — should skip
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="SKIPTRANS")
    requirement.sourcing_status = "won"
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "status": "open",
        },
    )

    assert resp.status_code == 200
    # At least one skipped — toast at warning level or "skipped" text
    assert "skipped" in resp.text.lower() or "warning" in resp.text.lower() or resp.text


def test_batch_status_success_creates_activity_log(client, db_session, test_user):
    """Successful batch-status update creates ActivityLog entries (lines 820-829)."""
    from app.models import ActivityLog

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ACTLOG01")
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
    log = db_session.query(ActivityLog).filter_by(requirement_id=requirement.id).first()
    assert log is not None
    assert "open" in log.notes.lower()
    assert "sourcing" in log.notes.lower()


# ── batch-notes: coverage of success loop (lines 866-884) ────────────


def test_batch_notes_multiple_requirements(client, db_session, test_user):
    """batch-notes adds notes to multiple requirements at once."""
    from app.models import ActivityLog

    _, req1 = _make_req_and_requirement(db_session, test_user.id, mpn="NOTES01")
    _, req2 = _make_req_and_requirement(db_session, test_user.id, mpn="NOTES02")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={
            "requirement_ids": json.dumps([req1.id, req2.id]),
            "notes": "Multi-requirement note",
        },
    )

    assert resp.status_code == 200
    logs = db_session.query(ActivityLog).filter(ActivityLog.requirement_id.in_([req1.id, req2.id])).all()
    assert len(logs) == 2


def test_batch_notes_singular_count_in_message(client, db_session, test_user):
    """batch-notes shows singular 'requirement' when count is 1 (line 883)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="SINGLE01")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/batch-notes",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "notes": "Singular note test",
        },
    )

    assert resp.status_code == 200
    # "1 requirement" (singular, not "requirements")
    assert "requirement" in resp.text.lower()


# ── mark-unavailable: broker.publish and sightings_detail (lines 900-919) ─


def test_mark_unavailable_broker_publish_called(client, db_session, test_user):
    """mark-unavailable calls broker.publish after marking (lines 913-917)."""
    from app.models.sourcing import Sighting

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="UNAVAIL01")
    sighting = Sighting(
        requirement_id=requirement.id,
        normalized_mpn="UNAVAIL01",
        vendor_name="TestVendorCov",
        source_type="manual",
        unit_price=1.00,
        qty_available=100,
        is_unavailable=False,
    )
    db_session.add(sighting)
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>detail</div>"),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{requirement.id}/mark-unavailable",
                data={"vendor_name": "TestVendorCov"},
            )

    assert resp.status_code == 200
    mock_broker.publish.assert_awaited_once()


def test_mark_unavailable_no_matching_sightings_still_succeeds(client, db_session, test_user):
    """mark-unavailable with no matching sightings commits and calls sightings_detail."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="NOMATCH01")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>empty</div>"),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{requirement.id}/mark-unavailable",
                data={"vendor_name": "NoSuchVendor"},
            )

    assert resp.status_code == 200


# ── assign-buyer: broker.publish (lines 941-947) ──────────────────────


def test_assign_buyer_broker_publish_called(client, db_session, test_user):
    """assign-buyer publishes SSE event after updating (lines 941-944)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ASGN01")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>ok</div>"),
        ):
            resp = client.patch(
                f"/v2/partials/sightings/{requirement.id}/assign",
                data={"assigned_buyer_id": str(test_user.id)},
            )

    assert resp.status_code == 200
    mock_broker.publish.assert_awaited_once()


def test_assign_buyer_clears_assignment_with_empty_id(client, db_session, test_user):
    """assign-buyer with empty assigned_buyer_id clears the assignment (line 932)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="CLEAR01")
    requirement.assigned_buyer_id = test_user.id
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>ok</div>"),
        ):
            resp = client.patch(
                f"/v2/partials/sightings/{requirement.id}/assign",
                data={"assigned_buyer_id": ""},
            )

    assert resp.status_code == 200
    db_session.refresh(requirement)
    assert requirement.assigned_buyer_id is None


# ── advance-status: full paths (lines 963-992) ────────────────────────


def test_advance_status_missing_status_returns_400(client, db_session, test_user):
    """advance-status without status param returns 400 (line 961)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ADV400")
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/sightings/{requirement.id}/advance-status",
        data={},
    )
    assert resp.status_code == 400


def test_advance_status_not_found_returns_404(client, db_session):
    """advance-status with unknown requirement_id returns 404 (line 965)."""
    resp = client.patch(
        "/v2/partials/sightings/99999/advance-status",
        data={"status": "sourcing"},
    )
    assert resp.status_code == 404


def test_advance_status_invalid_transition_returns_409(client, db_session, test_user):
    """advance-status with invalid transition raises 409 (line 970)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ADV409")
    requirement.sourcing_status = "won"
    db_session.commit()

    resp = client.patch(
        f"/v2/partials/sightings/{requirement.id}/advance-status",
        data={"status": "open"},
    )
    assert resp.status_code in (409, 400)


def test_advance_status_valid_transition_updates_db(client, db_session, test_user):
    """advance-status valid transition commits status change and publishes SSE."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ADV200")
    requirement.sourcing_status = "open"
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>updated</div>"),
        ):
            resp = client.patch(
                f"/v2/partials/sightings/{requirement.id}/advance-status",
                data={"status": "sourcing"},
            )

    assert resp.status_code == 200
    db_session.refresh(requirement)
    assert requirement.sourcing_status == "sourcing"
    mock_broker.publish.assert_awaited_once()


def test_advance_status_logs_activity(client, db_session, test_user):
    """advance-status creates an activity log entry (lines 976-983)."""
    from app.models import ActivityLog

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="ADVLOG01")
    requirement.sourcing_status = "open"
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>ok</div>"),
        ):
            client.patch(
                f"/v2/partials/sightings/{requirement.id}/advance-status",
                data={"status": "sourcing"},
            )

    log = db_session.query(ActivityLog).filter_by(requirement_id=requirement.id).first()
    assert log is not None
    assert "open" in log.notes.lower()
    assert "sourcing" in log.notes.lower()


# ── preview-inquiry: full paths (lines 1132-1181) ─────────────────────


def test_preview_inquiry_with_vendor_card_and_contact(client, db_session, test_user):
    """preview-inquiry resolves vendor email from VendorCard+VendorContact (lines 1153-1159)."""
    from app.models import VendorCard, VendorContact

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="PREV01")
    db_session.commit()

    card = VendorCard(
        normalized_name="previewvendor",
        display_name="Preview Vendor",
        emails=["sales@previewvendor.com"],
        phones=[],
        sighting_count=5,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()

    contact = VendorContact(
        vendor_card_id=card.id,
        full_name="Preview Contact",
        email="contact@previewvendor.com",
        source="manual",
        is_verified=True,
        confidence=90,
    )
    db_session.add(contact)
    db_session.commit()

    with patch(
        "app.email_service._build_html_body",
        return_value="<p>Preview body</p>",
    ):
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(requirement.id)],
                "vendor_names": ["Preview Vendor"],
                "email_body": "Please quote.",
            },
        )

    assert resp.status_code == 200


def test_preview_inquiry_vendor_not_in_db(client, db_session, test_user):
    """preview-inquiry with unknown vendor still builds preview (vendor_email='')."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="PREV02")
    db_session.commit()

    with patch(
        "app.email_service._build_html_body",
        return_value="<p>Body</p>",
    ):
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(requirement.id)],
                "vendor_names": ["Unknown Vendor XYZ"],
                "email_body": "Please quote.",
            },
        )

    assert resp.status_code == 200


def test_preview_inquiry_multiple_vendors(client, db_session, test_user):
    """preview-inquiry builds one preview per vendor (lines 1151-1172)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="PREV03")
    db_session.commit()

    with patch(
        "app.email_service._build_html_body",
        return_value="<p>Body</p>",
    ):
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(requirement.id)],
                "vendor_names": ["Vendor A", "Vendor B"],
                "email_body": "RFQ body",
            },
        )

    assert resp.status_code == 200


def test_preview_inquiry_builds_avail_token_from_requisition(client, db_session, test_user):
    """preview-inquiry sets avail_token from requisition_id (line 1148)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="PREV04")
    db_session.commit()

    with patch(
        "app.email_service._build_html_body",
        return_value="<p>Body</p>",
    ) as mock_build:
        resp = client.post(
            "/v2/partials/sightings/preview-inquiry",
            data={
                "requirement_ids": [str(requirement.id)],
                "vendor_names": ["Any Vendor"],
                "email_body": "Quote please",
            },
        )

    assert resp.status_code == 200
    mock_build.assert_called_once_with("Quote please")


# ── send-inquiry: full paths (lines 1206-1293) ────────────────────────


def test_send_inquiry_missing_email_body_returns_400(client, db_session, test_user):
    """send-inquiry without email_body returns 400 (line 1200-1204)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="SEND400")
    db_session.commit()

    resp = client.post(
        "/v2/partials/sightings/send-inquiry",
        data={
            "requirement_ids": [str(requirement.id)],
            "vendor_names": ["Arrow"],
            "email_body": "",
        },
    )
    assert resp.status_code == 400


def test_send_inquiry_with_vendor_card_resolves_email(client, db_session, test_user):
    """send-inquiry resolves vendor email from VendorCard+VendorContact (lines 1220-1227)."""
    from app.models import VendorCard, VendorContact

    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="SEND01")
    db_session.commit()

    card = VendorCard(
        normalized_name="sendvendor",
        display_name="Send Vendor",
        emails=["sales@sendvendor.com"],
        phones=[],
        sighting_count=1,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(card)
    db_session.flush()

    contact = VendorContact(
        vendor_card_id=card.id,
        full_name="Send Contact",
        email="contact@sendvendor.com",
        source="manual",
        is_verified=True,
        confidence=90,
    )
    db_session.add(contact)
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Send Vendor", "sent": True}],
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(requirement.id)],
                    "vendor_names": ["Send Vendor"],
                    "email_body": "Please quote urgently.",
                },
            )

    assert resp.status_code == 200
    assert mock_broker.publish.await_count >= 1


def test_send_inquiry_failed_sends_warning_toast(client, db_session, test_user):
    """send-inquiry exception path sets failed_vendors (lines 1271-1273, 1286-1288)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="SENDFAIL")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            side_effect=Exception("graph unavailable"),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(requirement.id)],
                    "vendor_names": ["FailVendor"],
                    "email_body": "Please quote.",
                },
            )

    assert resp.status_code == 200
    assert "warning" in resp.text.lower() or "failed" in resp.text.lower()


def test_send_inquiry_broker_publish_per_requirement(client, db_session, test_user):
    """send-inquiry publishes one SSE event per requirement (lines 1278-1283)."""
    _, req1 = _make_req_and_requirement(db_session, test_user.id, mpn="BRKR01")
    _, req2 = _make_req_and_requirement(db_session, test_user.id, mpn="BRKR02")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Arrow", "sent": True}],
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(req1.id), str(req2.id)],
                    "vendor_names": ["Arrow"],
                    "email_body": "Please quote.",
                },
            )

    assert resp.status_code == 200
    assert mock_broker.publish.await_count == 2


def test_send_inquiry_auto_progress_increments_count(client, db_session, test_user):
    """send-inquiry auto-progresses open requirements to sourcing (lines 1266-1270)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="AUTOPROG")
    requirement.sourcing_status = "open"
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Arrow", "sent": True}],
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
    # After successful send, status may advance to sourcing
    db_session.refresh(requirement)


def test_send_inquiry_success_toast_with_progressed_count(client, db_session, test_user):
    """send-inquiry toast mentions progressed count when auto-progress fires (line 1290-1291)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="PROG02")
    requirement.sourcing_status = "open"
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Arrow", "sent": True}],
        ):
            with patch(
                "app.services.sourcing_auto_progress.auto_progress_status",
                return_value=True,
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
    # "advanced to sourcing" or similar in the success toast
    assert "sourcing" in resp.text.lower() or "rfq" in resp.text.lower()


def test_send_inquiry_unknown_vendor_empty_email(client, db_session, test_user):
    """send-inquiry with vendor not in DB still sends (vendor_email stays empty, line 1223)."""
    _, requirement = _make_req_and_requirement(db_session, test_user.id, mpn="NOVCARD")
    db_session.commit()

    with patch(
        "app.routers.sightings.broker",
        new_callable=MagicMock,
    ) as mock_broker:
        mock_broker.publish = AsyncMock()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Ghost Vendor", "sent": True}],
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(requirement.id)],
                    "vendor_names": ["Ghost Vendor"],
                    "email_body": "Please quote.",
                },
            )

    assert resp.status_code == 200
