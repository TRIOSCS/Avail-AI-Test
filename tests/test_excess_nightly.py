"""test_excess_nightly.py — Additional coverage tests for app/routers/excess.py.

Targets uncovered lines:
- 165-177: partial_import_preview — unsupported ext (400), file too large (400), no rows (400)
- 288: api_import no-rows branch (400)
- 312, 315: api_preview_import unsupported-ext and file-too-large branches
- 476: bid ownership check (item not in list)
- 527-546: htmx_solicit with empty item_ids or missing recipient_email → 400
- 594: partial_bid_form with item in wrong list → 404
- 620: partial_bid_list with item in wrong list → 404

Called by: pytest
Depends on: app/routers/excess.py, conftest fixtures
"""

import os

os.environ["TESTING"] = "1"

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.models import Company
from app.models.excess import Bid, ExcessLineItem, ExcessList
from tests.conftest import engine

_ = engine  # ensure tables exist


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_company(db, name="Test Seller"):
    co = Company(name=name, is_active=True)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_excess_list(db, company_id, owner_id, title="Test List"):
    el = ExcessList(
        company_id=company_id,
        owner_id=owner_id,
        title=title,
        status="active",
        total_line_items=0,
        created_at=datetime.now(timezone.utc),
    )
    db.add(el)
    db.commit()
    db.refresh(el)
    return el


def _make_line_item(db, excess_list_id, part_number="LM317T"):
    item = ExcessLineItem(
        excess_list_id=excess_list_id,
        part_number=part_number,
        quantity=100,
        condition="New",
        asking_price=0.50,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_bid(db, line_item_id, user_id):
    bid = Bid(
        excess_line_item_id=line_item_id,
        unit_price=0.40,
        quantity_wanted=50,
        status="pending",
        created_by=user_id,
    )
    db.add(bid)
    db.commit()
    db.refresh(bid)
    return bid


# ── partial_import_preview (HTMX) ─────────────────────────────────────────────


class TestPartialImportPreview:
    """Lines 165-177: HTMX import preview validation branches."""

    def test_unsupported_file_type_returns_400(self, client, db_session, test_user):
        """Uploading .pdf to HTMX import-preview returns 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/v2/partials/excess/{el.id}/import-preview",
            files={"file": ("data.pdf", b"fake pdf", "application/pdf")},
        )
        assert resp.status_code == 400

    def test_file_too_large_returns_400(self, client, db_session, test_user):
        """File exceeding MAX_UPLOAD_BYTES returns 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        big = b"x" * (11 * 1024 * 1024)  # 11 MB > 10 MB limit
        resp = client.post(
            f"/v2/partials/excess/{el.id}/import-preview",
            files={"file": ("data.csv", big, "text/csv")},
        )
        assert resp.status_code == 400

    def test_no_data_rows_returns_400(self, client, db_session, test_user):
        """CSV with header only (no data rows) returns 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        # Header row only — parse_tabular_file returns []
        csv_content = b"part_number,quantity,manufacturer\n"
        with patch("app.routers.excess.parse_tabular_file", return_value=[]):
            resp = client.post(
                f"/v2/partials/excess/{el.id}/import-preview",
                files={"file": ("data.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 400


# ── api_import no-rows branch ─────────────────────────────────────────────────


class TestApiImportNoRows:
    """Line 288: empty parse result in /api/excess-lists/{id}/import returns 400."""

    def test_no_rows_found_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        csv_content = b"part_number,quantity,manufacturer\n"
        with patch("app.routers.excess.parse_tabular_file", return_value=[]):
            resp = client.post(
                f"/api/excess-lists/{el.id}/import",
                files={"file": ("data.csv", csv_content, "text/csv")},
            )
        assert resp.status_code == 400
        assert "No data rows" in resp.json()["error"]


# ── api_preview_import validation branches ────────────────────────────────────


class TestApiPreviewImportValidation:
    """Lines 312, 315: api_preview_import file-type and size validation."""

    def test_unsupported_extension_returns_400(self, client, db_session, test_user):
        """Line 312: unsupported extension returns 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/preview-import",
            files={"file": ("data.docx", b"fake", "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert "Unsupported" in resp.json()["error"]

    def test_file_too_large_returns_400(self, client, db_session, test_user):
        """Line 315: file size exceeding limit returns 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        big = b"x" * (11 * 1024 * 1024)
        resp = client.post(
            f"/api/excess-lists/{el.id}/preview-import",
            files={"file": ("data.csv", big, "text/csv")},
        )
        assert resp.status_code == 400
        assert "too large" in resp.json()["error"].lower()

    def test_no_rows_returns_400(self, client, db_session, test_user):
        """api_preview_import with empty parse result returns 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        with patch("app.routers.excess.parse_tabular_file", return_value=[]):
            resp = client.post(
                f"/api/excess-lists/{el.id}/preview-import",
                files={"file": ("data.csv", b"header\n", "text/csv")},
            )
        assert resp.status_code == 400
        assert "No data rows" in resp.json()["error"]


# ── bid ownership check (line 476) ────────────────────────────────────────────


class TestBidOwnershipCheck:
    """Line 476: PATCH bid with item that does not belong to the given list."""

    def test_item_not_in_list_returns_404(self, client, db_session, test_user):
        co = _make_company(db_session)
        el1 = _make_excess_list(db_session, co.id, test_user.id, "List A")
        el2 = _make_excess_list(db_session, co.id, test_user.id, "List B")
        item1 = _make_line_item(db_session, el1.id, "LM317T")
        bid = _make_bid(db_session, item1.id, test_user.id)

        # Use list 2 with item from list 1 — item ownership check fails
        resp = client.patch(
            f"/api/excess-lists/{el2.id}/line-items/{item1.id}/bids/{bid.id}",
            json={"notes": "wrong list"},
        )
        assert resp.status_code == 404
        assert "not found in list" in resp.json()["error"]


# ── htmx_solicit missing fields ────────────────────────────────────────────────


class TestHtmxSolicitations:
    """Lines 527-546: htmx_solicit validation and happy path."""

    def test_missing_item_ids_returns_400(self, client, db_session, test_user):
        """Empty item_ids with no recipient_email → 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/v2/partials/excess/{el.id}/solicit",
            data={"recipient_email": "buyer@test.com"},
        )
        assert resp.status_code == 400

    def test_missing_recipient_email_returns_400(self, client, db_session, test_user):
        """Valid item_ids but empty recipient_email → 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        resp = client.post(
            f"/v2/partials/excess/{el.id}/solicit",
            data={"item_ids": str(item.id)},
        )
        assert resp.status_code == 400

    def test_both_missing_returns_400(self, client, db_session, test_user):
        """Both item_ids and recipient_email missing → 400."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(f"/v2/partials/excess/{el.id}/solicit", data={})
        assert resp.status_code == 400

    def test_valid_solicitation_succeeds(self, client, db_session, test_user):
        """Lines 527-546: valid form data calls send_bid_solicitation and returns HTML."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)

        with patch("app.routers.excess.send_bid_solicitation", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = []
            resp = client.post(
                f"/v2/partials/excess/{el.id}/solicit",
                data={
                    "item_ids": str(item.id),
                    "recipient_email": "buyer@test.com",
                    "recipient_name": "John Buyer",
                    "subject": "Excess Offer",
                    "message": "Please review our excess inventory.",
                },
            )
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


# ── partial_bid_form wrong-list branch (line 594) ─────────────────────────────


class TestPartialBidFormWrongList:
    """Line 594: GET bid-form with item belonging to a different list returns 404."""

    def test_item_from_wrong_list_returns_404(self, client, db_session, test_user):
        co = _make_company(db_session)
        el1 = _make_excess_list(db_session, co.id, test_user.id, "List A")
        el2 = _make_excess_list(db_session, co.id, test_user.id, "List B")
        item = _make_line_item(db_session, el1.id)

        resp = client.get(f"/v2/partials/excess/{el2.id}/line-items/{item.id}/bid-form")
        assert resp.status_code == 404

    def test_nonexistent_item_returns_404(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)

        resp = client.get(f"/v2/partials/excess/{el.id}/line-items/99999/bid-form")
        assert resp.status_code == 404


# ── partial_bid_list wrong-list branch (line 620) ─────────────────────────────


class TestPartialBidListWrongList:
    """Line 620: GET bid-list with item belonging to a different list returns 404."""

    def test_item_from_wrong_list_returns_404(self, client, db_session, test_user):
        co = _make_company(db_session)
        el1 = _make_excess_list(db_session, co.id, test_user.id, "List A")
        el2 = _make_excess_list(db_session, co.id, test_user.id, "List B")
        item = _make_line_item(db_session, el1.id)

        resp = client.get(f"/v2/partials/excess/{el2.id}/line-items/{item.id}/bids")
        assert resp.status_code == 404

    def test_nonexistent_item_returns_404(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)

        resp = client.get(f"/v2/partials/excess/{el.id}/line-items/99999/bids")
        assert resp.status_code == 404


# ── partial_solicit_form with empty item_ids ──────────────────────────────────


class TestPartialSolicitFormEmptyItems:
    """Lines 527-546 context: partial_solicit_form with empty item_ids returns
    empty items list."""

    def test_empty_item_ids_returns_empty_items(self, client, db_session, test_user):
        """item_ids='' results in ids=[] and items=[]."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get(f"/v2/partials/excess/{el.id}/solicit-form?item_ids=")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_invalid_item_ids_are_filtered(self, client, db_session, test_user):
        """Non-numeric item_ids are silently ignored."""
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get(f"/v2/partials/excess/{el.id}/solicit-form?item_ids=abc,xyz")
        assert resp.status_code == 200
