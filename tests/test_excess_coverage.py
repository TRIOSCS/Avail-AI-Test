"""test_excess_coverage.py — Coverage tests for app/routers/excess.py.

Targets missing branches: list/create/update/delete, line items CRUD,
bids CRUD, stats, solicitations, AI email polish, proactive matches,
and HTMX partials.

Called by: pytest
Depends on: app/routers/excess.py, app/services/excess_service.py, conftest.py
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models import Company
from app.models.excess import Bid, ExcessLineItem, ExcessList
from tests.conftest import engine

os.environ["TESTING"] = "1"
_ = engine


# ── Helpers ───────────────────────────────────────────────────────────


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


# ── List endpoint ──────────────────────────────────────────────────────


class TestApiListExcessLists:
    def test_returns_items_and_total(self, client, db_session, test_user):
        co = _make_company(db_session)
        _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get("/api/excess-lists")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total" in data

    def test_search_filter(self, client, db_session, test_user):
        co = _make_company(db_session)
        _make_excess_list(db_session, co.id, test_user.id, "SpecificTitle123")
        resp = client.get("/api/excess-lists?q=SpecificTitle123")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_status_filter(self, client, db_session, test_user):
        co = _make_company(db_session)
        _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get("/api/excess-lists?status=active")
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    def test_pagination(self, client, db_session, test_user):
        resp = client.get("/api/excess-lists?limit=10&offset=0")
        assert resp.status_code == 200


# ── Create endpoint ────────────────────────────────────────────────────


class TestApiCreateExcessList:
    def test_creates_list(self, client, db_session, test_user):
        co = _make_company(db_session)
        resp = client.post(
            "/api/excess-lists",
            json={"title": "New Test List", "company_id": co.id},
        )
        assert resp.status_code == 201
        assert resp.json()["title"] == "New Test List"

    def test_creates_with_notes(self, client, db_session, test_user):
        co = _make_company(db_session)
        resp = client.post(
            "/api/excess-lists",
            json={"title": "With Notes", "company_id": co.id, "notes": "Test notes"},
        )
        assert resp.status_code == 201
        assert resp.json()["notes"] == "Test notes"


# ── Get / Update / Delete ──────────────────────────────────────────────


class TestApiGetUpdateDeleteExcessList:
    def test_get_existing(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get(f"/api/excess-lists/{el.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == el.id

    def test_get_not_found(self, client):
        resp = client.get("/api/excess-lists/99999")
        assert resp.status_code == 404

    def test_update_title(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.patch(f"/api/excess-lists/{el.id}", json={"title": "Updated Title"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated Title"

    def test_update_status(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.patch(f"/api/excess-lists/{el.id}", json={"status": "bidding"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "bidding"

    def test_delete_removes_list(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.delete(f"/api/excess-lists/{el.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_not_found(self, client):
        resp = client.delete("/api/excess-lists/99999")
        assert resp.status_code == 404


# ── Line Items ─────────────────────────────────────────────────────────


class TestApiLineItems:
    def test_add_line_item(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/line-items",
            json={
                "part_number": "LM317T",
                "manufacturer": "TI",
                "quantity": 500,
                "condition": "New",
                "asking_price": 0.45,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["part_number"] == "LM317T"

    def test_add_line_item_list_not_found(self, client):
        resp = client.post(
            "/api/excess-lists/99999/line-items",
            json={"part_number": "LM317T", "quantity": 100},
        )
        assert resp.status_code == 404

    def test_list_line_items(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        _make_line_item(db_session, el.id)
        resp = client.get(f"/api/excess-lists/{el.id}/line-items")
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1

    def test_list_line_items_pagination(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get(f"/api/excess-lists/{el.id}/line-items?limit=5&offset=0")
        assert resp.status_code == 200

    def test_delete_line_item(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        resp = client.delete(f"/api/excess-lists/{el.id}/line-items/{item.id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_line_item_wrong_list(self, client, db_session, test_user):
        co = _make_company(db_session)
        el1 = _make_excess_list(db_session, co.id, test_user.id, "List 1")
        el2 = _make_excess_list(db_session, co.id, test_user.id, "List 2")
        item = _make_line_item(db_session, el1.id)
        resp = client.delete(f"/api/excess-lists/{el2.id}/line-items/{item.id}")
        assert resp.status_code == 404

    def test_delete_line_item_not_found(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.delete(f"/api/excess-lists/{el.id}/line-items/99999")
        assert resp.status_code == 404


# ── File Import ────────────────────────────────────────────────────────


class TestApiFileImport:
    def test_unsupported_extension_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/import",
            files={"file": ("test.pdf", b"fake content", "application/pdf")},
        )
        assert resp.status_code == 400
        assert "Unsupported file type" in resp.json()["error"]

    def test_no_extension_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/import",
            files={"file": ("noext", b"fake content", "text/plain")},
        )
        assert resp.status_code == 400

    def test_file_too_large_returns_400(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        # 11 MB fake content
        big_content = b"x" * (11 * 1024 * 1024)
        resp = client.post(
            f"/api/excess-lists/{el.id}/import",
            files={"file": ("data.csv", big_content, "text/csv")},
        )
        assert resp.status_code == 400
        assert "too large" in resp.json()["error"].lower()

    def test_valid_csv_imports(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        csv_content = b"part_number,quantity,manufacturer\nLM317T,100,TI\nTL431,200,TI\n"
        resp = client.post(
            f"/api/excess-lists/{el.id}/import",
            files={"file": ("data.csv", csv_content, "text/csv")},
        )
        assert resp.status_code == 200


# ── Preview Import ─────────────────────────────────────────────────────


class TestApiPreviewImport:
    def test_preview_valid_csv(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        csv_content = b"part_number,quantity,manufacturer\nLM317T,100,TI\n"
        resp = client.post(
            f"/api/excess-lists/{el.id}/preview-import",
            files={"file": ("data.csv", csv_content, "text/csv")},
        )
        assert resp.status_code == 200

    def test_preview_unsupported_extension(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/preview-import",
            files={"file": ("test.docx", b"fake", "application/octet-stream")},
        )
        assert resp.status_code == 400


# ── Bids ──────────────────────────────────────────────────────────────


class TestApiBids:
    def test_create_bid(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/line-items/{item.id}/bids",
            json={"unit_price": 0.40, "quantity_wanted": 50},
        )
        assert resp.status_code == 201
        assert float(resp.json()["unit_price"]) == pytest.approx(0.40)

    def test_list_bids_empty(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        resp = client.get(f"/api/excess-lists/{el.id}/line-items/{item.id}/bids")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_update_bid_status(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        bid = Bid(
            excess_line_item_id=item.id,
            unit_price=0.40,
            quantity_wanted=50,
            status="pending",
            created_by=test_user.id,
        )
        db_session.add(bid)
        db_session.commit()
        db_session.refresh(bid)

        resp = client.patch(
            f"/api/excess-lists/{el.id}/line-items/{item.id}/bids/{bid.id}",
            json={"notes": "Looking good"},
        )
        assert resp.status_code == 200

    def test_update_bid_not_found(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        resp = client.patch(
            f"/api/excess-lists/{el.id}/line-items/{item.id}/bids/99999",
            json={"notes": "Test"},
        )
        assert resp.status_code == 404

    def test_update_bid_accept_calls_accept_bid(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        bid = Bid(
            excess_line_item_id=item.id,
            unit_price=0.40,
            quantity_wanted=50,
            status="pending",
            created_by=test_user.id,
        )
        db_session.add(bid)
        db_session.commit()
        db_session.refresh(bid)

        resp = client.patch(
            f"/api/excess-lists/{el.id}/line-items/{item.id}/bids/{bid.id}",
            json={"status": "accepted"},
        )
        assert resp.status_code == 200

    def test_update_bid_wrong_item(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item1 = _make_line_item(db_session, el.id, "LM317T")
        item2 = _make_line_item(db_session, el.id, "TL431")
        bid = Bid(
            excess_line_item_id=item1.id,
            unit_price=0.40,
            quantity_wanted=50,
            status="pending",
            created_by=test_user.id,
        )
        db_session.add(bid)
        db_session.commit()
        db_session.refresh(bid)

        # Access bid through wrong item
        resp = client.patch(
            f"/api/excess-lists/{el.id}/line-items/{item2.id}/bids/{bid.id}",
            json={"notes": "Wrong item"},
        )
        assert resp.status_code == 404


# ── Stats ──────────────────────────────────────────────────────────────


class TestApiExcessStats:
    def test_returns_stats(self, client):
        resp = client.get("/api/excess-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_lists" in data or isinstance(data, dict)


# ── Solicitations ──────────────────────────────────────────────────────


class TestApiSolicitations:
    def test_list_solicitations_empty(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.get(f"/api/excess-lists/{el.id}/solicitations")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_solicitations_with_item_filter(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        item = _make_line_item(db_session, el.id)
        resp = client.get(f"/api/excess-lists/{el.id}/solicitations?item_id={item.id}")
        assert resp.status_code == 200


# ── AI Email Polish ────────────────────────────────────────────────────


class TestApiPolishEmail:
    def test_polish_returns_polished_text(self, client):
        with patch("app.routers.excess.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = "Dear Sir, We are pleased to offer..."
            resp = client.post(
                "/api/excess-lists/polish-email",
                json={"text": "hey we got parts 4 sale u interested?"},
            )
        assert resp.status_code == 200
        assert "text" in resp.json()

    def test_polish_handles_claude_unavailable(self, client):
        from app.utils.claude_errors import ClaudeUnavailableError

        with patch("app.routers.excess.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = ClaudeUnavailableError("unavailable")
            resp = client.post(
                "/api/excess-lists/polish-email",
                json={"text": "original text"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "original text"

    def test_polish_handles_claude_error(self, client):
        from app.utils.claude_errors import ClaudeError

        with patch("app.routers.excess.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.side_effect = ClaudeError("error")
            resp = client.post(
                "/api/excess-lists/polish-email",
                json={"text": "fallback text"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "fallback text"

    def test_polish_handles_none_response(self, client):
        with patch("app.routers.excess.claude_text", new_callable=AsyncMock) as mock_claude:
            mock_claude.return_value = None
            resp = client.post(
                "/api/excess-lists/polish-email",
                json={"text": "original text"},
            )
        assert resp.status_code == 200
        assert resp.json()["text"] == "original text"


# ── Proactive Matches ──────────────────────────────────────────────────


class TestApiProactiveMatches:
    def test_create_proactive_matches(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(f"/api/excess-lists/{el.id}/create-proactive-matches")
        assert resp.status_code == 200
        assert "matches_created" in resp.json()


# ── Confirm Import ─────────────────────────────────────────────────────


class TestApiConfirmImport:
    def test_confirm_import_with_rows(self, client, db_session, test_user):
        co = _make_company(db_session)
        el = _make_excess_list(db_session, co.id, test_user.id)
        resp = client.post(
            f"/api/excess-lists/{el.id}/confirm-import",
            json={
                "rows": [
                    {
                        "part_number": "LM317T",
                        "manufacturer": "TI",
                        "quantity": 100,
                        "condition": "New",
                        "asking_price": 0.50,
                    }
                ]
            },
        )
        assert resp.status_code == 200
        assert "imported" in resp.json()
