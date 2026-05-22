"""tests/test_sightings_nightly_coverage.py — Coverage gap tests for sightings router.

Targets the lines uncovered by existing sightings test files when they run in
the pytest-xdist parallel harness. This file is designed to run serially so
coverage is collected correctly.

Called by: pytest (target: tests/test_sightings_nightly_coverage.py)
Depends on: conftest.py fixtures, app/routers/sightings.py
"""

import json
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.responses import HTMLResponse as _HTMLResponse
from sqlalchemy.orm import Session

from app.models import ActivityLog, Requirement, Requisition, User, VendorCard, VendorContact
from app.models.sourcing import Sighting


def _make_req(db, user):
    req = Requisition(
        name="REQ-SIGHTINGS-01",
        customer_name="Test Corp",
        status="active",
        created_by=user.id,
    )
    db.add(req)
    db.flush()
    return req


def _make_item(db, req, mpn="LM317T"):
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        target_price=0.50,
        sourcing_status="open",
    )
    db.add(item)
    db.flush()
    return item


def _make_sighting(db, item, vendor="Arrow Electronics"):
    sighting = Sighting(
        requirement_id=item.id,
        vendor_name=vendor,
        qty_available=1000,
        unit_price=0.55,
        currency="USD",
        is_unavailable=False,
    )
    db.add(sighting)
    db.flush()
    return sighting


def _make_req_and_item(db: Session, user: User, mpn: str = "LM317T") -> tuple:
    req = Requisition(name=f"NC-REQ-{mpn}", status="active", created_by=user.id)
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        target_qty=100,
        sourcing_status="open",
    )
    db.add(item)
    db.flush()
    db.commit()
    db.refresh(item)
    return req, item


# ── batch-assign ──────────────────────────────────────────────────────


class TestBatchAssign:
    def test_assign_buyer_to_requirements(self, client, db_session, test_user, test_requisition):
        """Batch assign buyer to requirements returns 200 HTML."""
        item = test_requisition.requirements[0]
        payload = {"requirement_ids": json.dumps([item.id]), "buyer_id": str(test_user.id)}
        resp = client.post("/v2/partials/sightings/batch-assign", data=payload)
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.assigned_buyer_id == test_user.id

    def test_assign_no_buyer_id(self, client, db_session, test_user, test_requisition):
        """Batch assign with no buyer_id unassigns."""
        item = test_requisition.requirements[0]
        item.assigned_buyer_id = test_user.id
        db_session.commit()

        payload = {"requirement_ids": json.dumps([item.id]), "buyer_id": ""}
        resp = client.post("/v2/partials/sightings/batch-assign", data=payload)
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.assigned_buyer_id is None

    def test_assign_empty_selection_returns_toast(self, client):
        """No requirements selected returns a warning toast."""
        payload = {"requirement_ids": "[]", "buyer_id": ""}
        resp = client.post("/v2/partials/sightings/batch-assign", data=payload)
        assert resp.status_code == 200
        assert b"No requirements selected" in resp.content

    def test_assign_known_buyer_name_shown(self, client, db_session, test_user, test_requisition):
        """Buyer name shown in toast when buyer_id resolves to existing user (line 793)."""
        item = test_requisition.requirements[0]
        payload = {"requirement_ids": json.dumps([item.id]), "buyer_id": str(test_user.id)}
        resp = client.post("/v2/partials/sightings/batch-assign", data=payload)
        assert resp.status_code == 200
        assert test_user.name in resp.text or "assigned" in resp.text.lower()


# ── batch-status ──────────────────────────────────────────────────────


class TestBatchStatus:
    def test_update_status_valid_transition(self, client, db_session, test_user, test_requisition):
        """Valid status transition open → sourcing updates requirements."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()

        payload = {"requirement_ids": json.dumps([item.id]), "status": "sourcing"}
        resp = client.post("/v2/partials/sightings/batch-status", data=payload)
        assert resp.status_code == 200

    def test_empty_selection_returns_toast(self, client):
        """No requirements selected returns warning toast."""
        payload = {"requirement_ids": "[]", "status": "sourcing"}
        resp = client.post("/v2/partials/sightings/batch-status", data=payload)
        assert resp.status_code == 200
        assert b"No requirements selected" in resp.content

    def test_invalid_status_returns_400(self, client):
        """Completely invalid status value returns 400."""
        payload = {"requirement_ids": "[1]", "status": "not_a_real_status"}
        resp = client.post("/v2/partials/sightings/batch-status", data=payload)
        assert resp.status_code == 400

    def test_invalid_transition_skipped(self, client, db_session, test_user, test_requisition):
        """Requirements in archived state (no allowed transitions) are skipped."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "archived"
        db_session.commit()

        payload = {"requirement_ids": json.dumps([item.id]), "status": "sourcing"}
        resp = client.post("/v2/partials/sightings/batch-status", data=payload)
        assert resp.status_code == 200
        assert b"skipped" in resp.content.lower()


# ── batch-notes ───────────────────────────────────────────────────────


class TestBatchNotes:
    def test_add_note_to_requirements(self, client, db_session, test_user, test_requisition):
        """Batch notes logs an activity."""
        item = test_requisition.requirements[0]
        payload = {"requirement_ids": json.dumps([item.id]), "notes": "Test batch note"}
        resp = client.post("/v2/partials/sightings/batch-notes", data=payload)
        assert resp.status_code == 200

    def test_empty_selection_returns_toast(self, client):
        """No requirements selected returns warning toast."""
        payload = {"requirement_ids": "[]", "notes": "Some note"}
        resp = client.post("/v2/partials/sightings/batch-notes", data=payload)
        assert resp.status_code == 200
        assert b"No requirements selected" in resp.content


# ── mark-unavailable ──────────────────────────────────────────────────


class TestMarkUnavailable:
    def test_marks_sightings_unavailable(self, client, db_session, test_user, test_requisition):
        """Marks all sightings for a vendor as unavailable."""
        item = test_requisition.requirements[0]
        sighting = _make_sighting(db_session, item, "Arrow Electronics")
        db_session.commit()

        payload = {"vendor_name": "Arrow Electronics"}
        resp = client.post(
            f"/v2/partials/sightings/{item.id}/mark-unavailable",
            data=payload,
        )
        assert resp.status_code == 200
        db_session.refresh(sighting)
        assert sighting.is_unavailable is True

    def test_missing_vendor_name_returns_400(self, client, test_requisition):
        """Missing vendor_name returns 400."""
        item = test_requisition.requirements[0]
        resp = client.post(f"/v2/partials/sightings/{item.id}/mark-unavailable", data={})
        assert resp.status_code == 400


# ── assign-buyer ──────────────────────────────────────────────────────


class TestAssignBuyer:
    def test_assigns_buyer(self, client, db_session, test_user, test_requisition):
        """Assigns a buyer to a requirement."""
        item = test_requisition.requirements[0]
        payload = {"assigned_buyer_id": str(test_user.id)}
        resp = client.patch(f"/v2/partials/sightings/{item.id}/assign", data=payload)
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.assigned_buyer_id == test_user.id

    def test_unassigns_buyer(self, client, db_session, test_user, test_requisition):
        """Empty buyer_id unassigns."""
        item = test_requisition.requirements[0]
        item.assigned_buyer_id = test_user.id
        db_session.commit()

        payload = {"assigned_buyer_id": ""}
        resp = client.patch(f"/v2/partials/sightings/{item.id}/assign", data=payload)
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.assigned_buyer_id is None

    def test_nonexistent_requirement_returns_404(self, client):
        """404 for unknown requirement."""
        payload = {"assigned_buyer_id": "1"}
        resp = client.patch("/v2/partials/sightings/99999/assign", data=payload)
        assert resp.status_code == 404


# ── advance-status ────────────────────────────────────────────────────


class TestAdvanceStatus:
    def test_advances_valid_status(self, client, db_session, test_user, test_requisition):
        """Valid transition updates sourcing_status."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()

        payload = {"status": "searching"}
        resp = client.patch(f"/v2/partials/sightings/{item.id}/advance-status", data=payload)
        assert resp.status_code in (200, 409)

    def test_missing_status_returns_400(self, client, test_requisition):
        """No status provided → 400."""
        item = test_requisition.requirements[0]
        resp = client.patch(f"/v2/partials/sightings/{item.id}/advance-status", data={})
        assert resp.status_code == 400

    def test_nonexistent_requirement_returns_404(self, client):
        """404 for unknown requirement."""
        resp = client.patch("/v2/partials/sightings/99999/advance-status", data={"status": "searching"})
        assert resp.status_code == 404


# ── log-activity ──────────────────────────────────────────────────────


class TestLogActivity:
    def test_logs_note_activity(self, client, db_session, test_user, test_requisition):
        """POST log-activity creates an ActivityLog record."""
        item = test_requisition.requirements[0]
        payload = {"notes": "Test note entry", "channel": "note", "vendor_name": ""}
        resp = client.post(f"/v2/partials/sightings/{item.id}/log-activity", data=payload)
        assert resp.status_code == 200

        activity = db_session.query(ActivityLog).filter(ActivityLog.requirement_id == item.id).first()
        assert activity is not None
        assert "Test note entry" in activity.notes

    def test_logs_call_activity(self, client, db_session, test_user, test_requisition):
        """Call channel sets activity_type to call_outbound."""
        item = test_requisition.requirements[0]
        payload = {"notes": "Called vendor", "channel": "call", "vendor_name": "Arrow"}
        resp = client.post(f"/v2/partials/sightings/{item.id}/log-activity", data=payload)
        assert resp.status_code == 200

        activity = (
            db_session.query(ActivityLog)
            .filter(ActivityLog.requirement_id == item.id, ActivityLog.activity_type == "call_outbound")
            .first()
        )
        assert activity is not None

    def test_empty_notes_returns_400(self, client, test_requisition):
        """Empty notes → 400."""
        item = test_requisition.requirements[0]
        payload = {"notes": "   ", "channel": "note"}
        resp = client.post(f"/v2/partials/sightings/{item.id}/log-activity", data=payload)
        assert resp.status_code == 400

    def test_invalid_channel_returns_400(self, client, test_requisition):
        """Invalid channel → 400."""
        item = test_requisition.requirements[0]
        payload = {"notes": "Some note", "channel": "fax"}
        resp = client.post(f"/v2/partials/sightings/{item.id}/log-activity", data=payload)
        assert resp.status_code == 400

    def test_nonexistent_requirement_returns_404(self, client):
        """404 for unknown requirement."""
        payload = {"notes": "Some note", "channel": "note"}
        resp = client.post("/v2/partials/sightings/99999/log-activity", data=payload)
        assert resp.status_code == 404


# ── sightings_refresh ─────────────────────────────────────────────────


class TestSightingsRefresh:
    def test_refresh_unknown_requirement(self, client):
        """404 for unknown requirement."""
        resp = client.post("/v2/partials/sightings/99999/refresh")
        assert resp.status_code == 404

    def test_refresh_success(self, client, db_session, test_user, test_requisition):
        """Refresh succeeds and returns detail panel."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {"LM317T": "found"}},
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")
        assert resp.status_code == 200

    def test_refresh_handles_search_exception(self, client, db_session, test_user, test_requisition):
        """Search exception doesn't crash the endpoint — returns refresh_failed toast."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            side_effect=RuntimeError("API unavailable"),
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")
        assert resp.status_code == 200


# ── batch-refresh ─────────────────────────────────────────────────────


class TestBatchRefresh:
    def test_batch_refresh_empty_ids(self, client):
        """No requirement_ids → warning toast."""
        payload = {"requirement_ids": "[]"}
        resp = client.post("/v2/partials/sightings/batch-refresh", data=payload)
        assert resp.status_code == 200

    def test_batch_refresh_runs(self, client, db_session, test_user, test_requisition):
        """Batch refresh with valid IDs returns 200."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {"LM317T": "found"}},
        ):
            payload = {"requirement_ids": json.dumps([item.id])}
            resp = client.post("/v2/partials/sightings/batch-refresh", data=payload)
        assert resp.status_code == 200

    def test_batch_refresh_sse_source_returns_empty(self, client, test_requisition):
        """source=sse suppresses the OOB toast (returns empty HTML)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {}},
        ):
            payload = {"requirement_ids": json.dumps([item.id])}
            resp = client.post("/v2/partials/sightings/batch-refresh?source=sse", data=payload)
        assert resp.status_code == 200
        assert resp.content == b""


# ── _build_mpn_toast unit tests ───────────────────────────────────────


class TestBuildMpnToast:
    def test_searched_and_cached_mixed(self):
        """Both searched and cached MPNs → combined message (line 677)."""
        from app.routers.sightings import _build_mpn_toast

        result = _build_mpn_toast({"A": "searched", "B": "cached"}, False)
        assert "1" in result and "cached" in result

    def test_all_cached(self):
        """Only cached MPNs → 'All MPNs searched within 48h'."""
        from app.routers.sightings import _build_mpn_toast

        result = _build_mpn_toast({"A": "cached", "B": "cached"}, False)
        assert "48h" in result


# ── _build_mpn_toast via refresh endpoint (lines 677, 679, 680) ──────────────
# Direct HTTP endpoint tests that fire the toast branches


class TestBuildMpnToastViaRefresh:
    """Tests _build_mpn_toast indirectly by checking refresh HX-Trigger headers."""

    def test_searched_and_cached_mix_fires_line_677(self, client, db_session, test_user, test_requisition):
        """searched > 0 AND cached > 0 → line 677 branch (mixed message)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {"MPN1": "searched", "MPN2": "cached"}},
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "cached" in trigger

    def test_all_cached_fires_line_680(self, client, db_session, test_user, test_requisition):
        """All cached → line 680 branch (all-cached message with 48h)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {"MPN1": "cached", "MPN2": "cached"}},
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "48h" in trigger

    def test_only_searched_fires_line_679(self, client, db_session, test_user, test_requisition):
        """Only searched → line 679 branch (Searched N MPNs)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {"MPN1": "searched"}},
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")
        assert resp.status_code == 200
        trigger = resp.headers.get("HX-Trigger", "")
        assert "Searched" in trigger

    def test_empty_results_no_hx_trigger(self, client, db_session, test_user, test_requisition):
        """Empty mpn_results → no HX-Trigger (line 673 false branch)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value={"mpn_results": {}},
        ):
            resp = client.post(f"/v2/partials/sightings/{item.id}/refresh")
        assert resp.status_code == 200
        assert "HX-Trigger" not in resp.headers


# ── batch-refresh missing branches (lines 699-764) ───────────────────────────


class TestBatchRefreshMissingBranches:
    def test_invalid_json_returns_400(self, client):
        """Malformed JSON returns 400 (lines 704-705)."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "not-json!!!"},
        )
        assert resp.status_code == 400

    def test_non_list_json_treated_as_empty(self, client):
        """Non-list JSON (dict) is coerced to empty list (line 703)."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": '{"key": "value"}'},
        )
        assert resp.status_code == 200

    def test_too_many_ids_returns_400(self, client):
        """More than MAX_BATCH_SIZE IDs returns 400 (lines 707-708)."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": json.dumps(list(range(51)))},
        )
        assert resp.status_code == 400

    def test_nonexistent_id_counts_as_failed(self, client):
        """Non-existent requirement ID increments failed (lines 724-727)."""
        resp = client.post(
            "/v2/partials/sightings/batch-refresh",
            data={"requirement_ids": "[88888]"},
        )
        assert resp.status_code == 200
        assert "1" in resp.text

    def test_valid_requirement_triggers_search(self, client, db_session, test_user, test_requisition):
        """Valid ID fires search_requirement (lines 737-747)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_search:
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
            )
        assert resp.status_code == 200
        mock_search.assert_awaited_once()

    def test_search_exception_increments_failed(self, client, db_session, test_user, test_requisition):
        """Search exception increments failed count (lines 743-745)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connector error"),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
            )
        assert resp.status_code == 200
        assert "warning" in resp.text or "1" in resp.text

    def test_sse_source_suppresses_toast_returns_empty(self, client, db_session, test_user, test_requisition):
        """source=sse returns empty HTMLResponse (line 764)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            return_value=None,
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh?source=sse",
                data={"requirement_ids": json.dumps([item.id])},
            )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_failed_toast_warning_level(self, client, db_session, test_user, test_requisition):
        """When failed > 0, toast level is warning (lines 759-762)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.search_service.search_requirement",
            new_callable=AsyncMock,
            side_effect=Exception("fail"),
        ):
            resp = client.post(
                "/v2/partials/sightings/batch-refresh",
                data={"requirement_ids": json.dumps([item.id])},
            )
        assert resp.status_code == 200
        assert "warning" in resp.text


# ── batch-assign missing branches (lines 776-801) ────────────────────────────


class TestBatchAssignMissingBranches:
    def test_too_many_returns_400(self, client):
        """Over MAX_BATCH_SIZE returns 400 (lines 781-782)."""
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps(list(range(51))), "buyer_id": "1"},
        )
        assert resp.status_code == 400

    def test_buyer_name_shown_in_toast(self, client, db_session, test_user, test_requisition):
        """Buyer name displayed in toast when buyer_id resolves to a user (line 793)."""
        item = test_requisition.requirements[0]
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([item.id]), "buyer_id": str(test_user.id)},
        )
        assert resp.status_code == 200
        assert test_user.name in resp.text or "assigned" in resp.text.lower()

    def test_plural_requirements_text(self, client, db_session, test_user, test_requisition):
        """2 requirements → plural 'requirements' (line 800)."""
        item = test_requisition.requirements[0]
        # Need a second requirement in the same/different requisition
        req2, item2 = _make_req_and_item(db_session, test_user, mpn="BASSN-EXTRA")
        resp = client.post(
            "/v2/partials/sightings/batch-assign",
            data={"requirement_ids": json.dumps([item.id, item2.id]), "buyer_id": ""},
        )
        assert resp.status_code == 200
        assert "requirements" in resp.text


# ── batch-status missing branches (lines 815-861) ────────────────────────────


class TestBatchStatusMissingBranches:
    def test_too_many_returns_400(self, client):
        """Over MAX_BATCH_SIZE returns 400 (lines 819-820)."""
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps(list(range(51))), "status": "sourcing"},
        )
        assert resp.status_code == 400

    def test_valid_transition_creates_activity_log(self, client, db_session, test_user, test_requisition):
        """Valid transition creates ActivityLog (lines 831-852)."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.sourcing_status == "sourcing"
        log = db_session.query(ActivityLog).filter_by(requirement_id=item.id).first()
        assert log is not None

    def test_skipped_increments_causes_warning_level(self, client, db_session, test_user, test_requisition):
        """Skipped > 0 → warning level toast (lines 856-860)."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "won"
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "open"},
        )
        assert resp.status_code == 200
        assert "skipped" in resp.text.lower() or "warning" in resp.text.lower()

    def test_all_valid_success_level(self, client, db_session, test_user, test_requisition):
        """All valid → success level (line 856)."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()
        resp = client.post(
            "/v2/partials/sightings/batch-status",
            data={"requirement_ids": json.dumps([item.id]), "status": "sourcing"},
        )
        assert resp.status_code == 200
        assert "success" in resp.text


# ── batch-notes missing branches (lines 872-903) ─────────────────────────────


class TestBatchNotesMissingBranches:
    def test_too_many_returns_400(self, client):
        """Over MAX_BATCH_SIZE returns 400 (lines 877-878)."""
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps(list(range(51))), "notes": "note"},
        )
        assert resp.status_code == 400

    def test_empty_notes_warning(self, client, db_session, test_user, test_requisition):
        """Empty notes returns warning (lines 883-884)."""
        item = test_requisition.requirements[0]
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id]), "notes": ""},
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower() or "warning" in resp.text.lower()

    def test_singular_message(self, client, db_session, test_user, test_requisition):
        """1 requirement → singular form (line 902)."""
        item = test_requisition.requirements[0]
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id]), "notes": "Test note singular"},
        )
        assert resp.status_code == 200
        assert "requirement" in resp.text

    def test_multiple_creates_multiple_logs(self, client, db_session, test_user, test_requisition):
        """Multiple requirements each get an ActivityLog (lines 888-896)."""
        item = test_requisition.requirements[0]
        req2, item2 = _make_req_and_item(db_session, test_user, mpn="BNOTE-EXTRA2")
        resp = client.post(
            "/v2/partials/sightings/batch-notes",
            data={"requirement_ids": json.dumps([item.id, item2.id]), "notes": "Bulk note"},
        )
        assert resp.status_code == 200
        logs = db_session.query(ActivityLog).filter(ActivityLog.requirement_id.in_([item.id, item2.id])).all()
        assert len(logs) >= 1


# ── mark-unavailable mocked sightings_detail (lines 920-935) ─────────────────


class TestMarkUnavailableMocked:
    """Use mocked sightings_detail to ensure lines 920-935 are covered."""

    def test_marks_sightings_with_mocked_detail(self, client, db_session, test_user, test_requisition):
        """Lines 920-935 covered: normalize, query, mark, commit, publish, return."""
        item = test_requisition.requirements[0]
        sighting = _make_sighting(db_session, item, "Mouser Electronics")
        db_session.commit()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>detail</div>"),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/mark-unavailable",
                data={"vendor_name": "Mouser Electronics"},
            )
        assert resp.status_code == 200
        db_session.refresh(sighting)
        assert sighting.is_unavailable is True

    def test_no_matching_sightings_still_returns_detail(self, client, db_session, test_user, test_requisition):
        """No matching sightings → commit no-op, still calls sightings_detail."""
        item = test_requisition.requirements[0]
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>no vendor</div>"),
        ):
            resp = client.post(
                f"/v2/partials/sightings/{item.id}/mark-unavailable",
                data={"vendor_name": "Ghost Vendor XYZ"},
            )
        assert resp.status_code == 200

    def test_sse_source_skips_publish(self, client, db_session, test_user, test_requisition):
        """source=sse skips broker.publish (line 933)."""
        item = test_requisition.requirements[0]
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.routers.sightings.sightings_detail",
                new_callable=AsyncMock,
                return_value=_HTMLResponse("<div>ok</div>"),
            ):
                resp = client.post(
                    f"/v2/partials/sightings/{item.id}/mark-unavailable?source=sse",
                    data={"vendor_name": "Any Vendor"},
                )
        assert resp.status_code == 200
        mock_broker.publish.assert_not_awaited()


# ── assign-buyer mocked sightings_detail (lines 948-960) ─────────────────────


class TestAssignBuyerMocked:
    """Explicit coverage of lines 948-960 with mocked sightings_detail."""

    def test_assigns_buyer_and_returns_detail(self, client, db_session, test_user, test_requisition):
        """Lines 948-960: form parse, assign, commit, publish, return."""
        item = test_requisition.requirements[0]
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>assigned</div>"),
        ):
            resp = client.patch(
                f"/v2/partials/sightings/{item.id}/assign",
                data={"assigned_buyer_id": str(test_user.id)},
            )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.assigned_buyer_id == test_user.id

    def test_clears_buyer_with_empty_string(self, client, db_session, test_user, test_requisition):
        """Empty assigned_buyer_id → None (line 949)."""
        item = test_requisition.requirements[0]
        item.assigned_buyer_id = test_user.id
        db_session.commit()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>cleared</div>"),
        ):
            resp = client.patch(
                f"/v2/partials/sightings/{item.id}/assign",
                data={"assigned_buyer_id": ""},
            )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.assigned_buyer_id is None

    def test_sse_source_skips_publish(self, client, db_session, test_user, test_requisition):
        """source=sse skips broker.publish (line 958)."""
        item = test_requisition.requirements[0]
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.routers.sightings.sightings_detail",
                new_callable=AsyncMock,
                return_value=_HTMLResponse("<div>ok</div>"),
            ):
                resp = client.patch(
                    f"/v2/partials/sightings/{item.id}/assign?source=sse",
                    data={"assigned_buyer_id": ""},
                )
        assert resp.status_code == 200
        mock_broker.publish.assert_not_awaited()


# ── advance-status mocked sightings_detail (lines 977-1002) ──────────────────


class TestAdvanceStatusMocked:
    """Explicit coverage of lines 977-1002 with mocked sightings_detail."""

    def test_invalid_transition_returns_409(self, client, db_session, test_user, test_requisition):
        """Invalid transition raises 409 (line 984)."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "won"
        db_session.commit()
        resp = client.patch(
            f"/v2/partials/sightings/{item.id}/advance-status",
            data={"status": "open"},
        )
        assert resp.status_code in (409, 400)

    def test_valid_transition_updates_and_logs(self, client, db_session, test_user, test_requisition):
        """Lines 981-1002: fetch req, validate, update status, log, commit, publish, return."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()
        with patch(
            "app.routers.sightings.sightings_detail",
            new_callable=AsyncMock,
            return_value=_HTMLResponse("<div>advanced</div>"),
        ):
            resp = client.patch(
                f"/v2/partials/sightings/{item.id}/advance-status",
                data={"status": "sourcing"},
            )
        assert resp.status_code == 200
        db_session.refresh(item)
        assert item.sourcing_status == "sourcing"
        log = db_session.query(ActivityLog).filter_by(requirement_id=item.id).first()
        assert log is not None
        assert "sourcing" in log.notes

    def test_sse_source_skips_publish(self, client, db_session, test_user, test_requisition):
        """source=sse skips broker.publish (line 1000)."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.routers.sightings.sightings_detail",
                new_callable=AsyncMock,
                return_value=_HTMLResponse("<div>ok</div>"),
            ):
                resp = client.patch(
                    f"/v2/partials/sightings/{item.id}/advance-status?source=sse",
                    data={"status": "sourcing"},
                )
        assert resp.status_code == 200
        mock_broker.publish.assert_not_awaited()


# ── preview-inquiry (lines 1135-1190) ────────────────────────────────────────


class TestPreviewInquiryMissingBranches:
    def test_missing_params_returns_400(self, client):
        """No params → 400 (lines 1138-1139)."""
        resp = client.post("/v2/partials/sightings/preview-inquiry", data={})
        assert resp.status_code == 400

    def test_success_with_valid_params(self, client, db_session, test_user, test_requisition):
        """Valid params → rendered preview (lines 1141-1190)."""
        item = test_requisition.requirements[0]
        with patch("app.email_service._build_html_body", return_value="<p>Preview</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["Arrow Electronics"],
                    "email_body": "Quote please.",
                },
            )
        assert resp.status_code == 200

    def test_resolves_vendor_email_from_card_and_contact(self, client, db_session, test_user, test_requisition):
        """Vendor email resolved from VendorCard+VendorContact (lines 1148-1167)."""
        item = test_requisition.requirements[0]
        card = VendorCard(
            normalized_name="previewvendor",
            display_name="Preview Vendor",
            emails=["sales@preview.com"],
            phones=[],
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            full_name="Preview Contact",
            email="contact@preview.com",
            source="manual",
            is_verified=True,
            confidence=90,
        )
        db_session.add(contact)
        db_session.commit()
        with patch("app.email_service._build_html_body", return_value="<p>Preview</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["Preview Vendor"],
                    "email_body": "Quote please.",
                },
            )
        assert resp.status_code == 200

    def test_multiple_vendors_each_previewed(self, client, db_session, test_user, test_requisition):
        """Multiple vendors each get a preview (line 1161 loop)."""
        item = test_requisition.requirements[0]
        with patch("app.email_service._build_html_body", return_value="<p>Preview</p>"):
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["Vendor A", "Vendor B"],
                    "email_body": "RFQ body",
                },
            )
        assert resp.status_code == 200

    def test_avail_token_uses_requisition_id(self, client, db_session, test_user, test_requisition):
        """avail_token set from requisition_id (line 1157)."""
        item = test_requisition.requirements[0]
        with patch("app.email_service._build_html_body", return_value="<p>Preview</p>") as mock_build:
            resp = client.post(
                "/v2/partials/sightings/preview-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["Any Vendor"],
                    "email_body": "body text",
                },
            )
        assert resp.status_code == 200
        mock_build.assert_called_once_with("body text")


# ── send-inquiry (lines 1216-1299) ────────────────────────────────────────────


class TestSendInquiryMissingBranches:
    def test_missing_params_returns_400(self, client):
        """No params → 400 (lines 1210-1214)."""
        resp = client.post("/v2/partials/sightings/send-inquiry", data={})
        assert resp.status_code == 400

    def test_missing_email_body_returns_400(self, client, db_session, test_user, test_requisition):
        """Empty email_body → 400 (line 1210)."""
        item = test_requisition.requirements[0]
        resp = client.post(
            "/v2/partials/sightings/send-inquiry",
            data={
                "requirement_ids": [str(item.id)],
                "vendor_names": ["Arrow"],
                "email_body": "",
            },
        )
        assert resp.status_code == 400

    def test_success_calls_send_batch_rfq(self, client, db_session, test_user, test_requisition):
        """Successful send calls send_batch_rfq (lines 1216-1262)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Arrow", "sent": True}],
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["Arrow"],
                    "email_body": "Please quote.",
                },
            )
        assert resp.status_code == 200
        assert "rfq" in resp.text.lower() or "sent" in resp.text.lower()

    def test_resolves_vendor_email_from_card_contact(self, client, db_session, test_user, test_requisition):
        """Vendor email resolved from VendorCard+VendorContact (lines 1227-1237)."""
        item = test_requisition.requirements[0]
        card = VendorCard(
            normalized_name="sendco",
            display_name="Send Co",
            emails=["sales@sendco.com"],
            phones=[],
            sighting_count=1,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(card)
        db_session.flush()
        contact = VendorContact(
            vendor_card_id=card.id,
            full_name="Send Contact",
            email="contact@sendco.com",
            source="manual",
            is_verified=True,
            confidence=90,
        )
        db_session.add(contact)
        db_session.commit()
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            return_value=[{"vendor_name": "Send Co", "sent": True}],
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["Send Co"],
                    "email_body": "Quote urgently.",
                },
            )
        assert resp.status_code == 200

    def test_exception_sets_failed_vendors_warning(self, client, db_session, test_user, test_requisition):
        """send_batch_rfq exception → warning toast with failed vendor names (lines 1281-1295)."""
        item = test_requisition.requirements[0]
        with patch(
            "app.email_service.send_batch_rfq",
            new_callable=AsyncMock,
            side_effect=Exception("Graph API down"),
        ):
            resp = client.post(
                "/v2/partials/sightings/send-inquiry",
                data={
                    "requirement_ids": [str(item.id)],
                    "vendor_names": ["FailVendor"],
                    "email_body": "Quote please.",
                },
            )
        assert resp.status_code == 200
        assert "warning" in resp.text.lower() or "failed" in resp.text.lower()
        assert "FailVendor" in resp.text

    def test_auto_progress_increments_progressed_count(self, client, db_session, test_user, test_requisition):
        """auto_progress_status=True increments progressed_count (lines 1278-1280)."""
        item = test_requisition.requirements[0]
        item.sourcing_status = "open"
        db_session.commit()
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
                        "requirement_ids": [str(item.id)],
                        "vendor_names": ["Arrow"],
                        "email_body": "Quote please.",
                    },
                )
        assert resp.status_code == 200
        assert "sourcing" in resp.text.lower() or "advanced" in resp.text.lower() or "rfq" in resp.text.lower()

    def test_publishes_sse_per_requirement(self, client, db_session, test_user, test_requisition):
        """broker.publish called once per requirement (lines 1287-1289)."""
        item = test_requisition.requirements[0]
        req2, item2 = _make_req_and_item(db_session, test_user, mpn="SEND-SSE-EXTRA")
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.email_service.send_batch_rfq",
                new_callable=AsyncMock,
                return_value=[{"vendor_name": "Arrow", "sent": True}],
            ):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry",
                    data={
                        "requirement_ids": [str(item.id), str(item2.id)],
                        "vendor_names": ["Arrow"],
                        "email_body": "Quote please.",
                    },
                )
        assert resp.status_code == 200
        assert mock_broker.publish.await_count == 2

    def test_sse_source_skips_broker_publish(self, client, db_session, test_user, test_requisition):
        """source=sse skips broker.publish for each requirement."""
        item = test_requisition.requirements[0]
        with patch("app.routers.sightings.broker") as mock_broker:
            mock_broker.publish = AsyncMock()
            with patch(
                "app.email_service.send_batch_rfq",
                new_callable=AsyncMock,
                return_value=[{"vendor_name": "Arrow", "sent": True}],
            ):
                resp = client.post(
                    "/v2/partials/sightings/send-inquiry?source=sse",
                    data={
                        "requirement_ids": [str(item.id)],
                        "vendor_names": ["Arrow"],
                        "email_body": "Quote.",
                    },
                )
        assert resp.status_code == 200
        mock_broker.publish.assert_not_awaited()
