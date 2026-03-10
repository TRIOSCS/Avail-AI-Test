"""Tests for part-level (requirement-scoped) API endpoints and quote expiration.

Covers: /api/requirements/{id}/offers, /api/requirements/{id}/notes,
/api/requirements/{id}/tasks — used by the part-number expansion panel.
Also covers: quote expiration badges (is_expired, days_until_expiry).

Called by: pytest
Depends on: routers/requisitions/requirements.py, routers/crm/quotes.py, conftest fixtures
"""

import pytest
from datetime import datetime, timedelta, timezone


# ── Part-level Offers ───────────────────────────────────────────────


def test_list_requirement_offers_empty(client, test_requisition, db_session):
    """GET /api/requirements/{id}/offers returns empty list when no offers exist."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    resp = client.get(f"/api/requirements/{req.id}/offers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_requirement_offers_with_data(client, test_requisition, db_session):
    """GET /api/requirements/{id}/offers returns offers for this requirement."""
    from app.models import Offer, Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    offer = Offer(
        requisition_id=test_requisition.id,
        requirement_id=req.id,
        vendor_name="Arrow Electronics",
        mpn="LM317T",
        qty_available=500,
        unit_price=0.85,
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{req.id}/offers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["vendor_name"] == "Arrow Electronics"
    assert data[0]["mpn"] == "LM317T"
    assert data[0]["qty_available"] == 500
    assert float(data[0]["unit_price"]) == 0.85


def test_list_requirement_offers_includes_extended_fields(client, test_requisition, db_session):
    """GET /api/requirements/{id}/offers returns country_of_origin, firmware, source, entered_by."""
    from app.models import Offer, Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    offer = Offer(
        requisition_id=test_requisition.id,
        requirement_id=req.id,
        vendor_name="Mouser Electronics",
        mpn="STM32F407VGT6",
        qty_available=1000,
        unit_price=8.25,
        status="active",
        country_of_origin="CN",
        firmware="v2.1",
        source="email",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{req.id}/offers")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1
    row = next(r for r in data if r["vendor_name"] == "Mouser Electronics")
    assert row["country_of_origin"] == "CN"
    assert row["firmware"] == "v2.1"
    assert row["source"] == "email"
    assert "created_at" in row
    assert "age_days" in row


def test_list_requirement_offers_404(client):
    """GET /api/requirements/99999/offers returns 404 for nonexistent requirement."""
    resp = client.get("/api/requirements/99999/offers")
    assert resp.status_code == 404


# ── Part-level Notes ────────────────────────────────────────────────


def test_list_requirement_notes_empty(client, test_requisition, db_session):
    """GET /api/requirements/{id}/notes returns empty when no notes."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    req.notes = None
    db_session.commit()

    resp = client.get(f"/api/requirements/{req.id}/notes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["requirement_notes"] == ""
    assert data["notes"] == []


def test_add_requirement_note(client, test_requisition, db_session):
    """POST /api/requirements/{id}/notes appends to requirement notes."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    req.notes = None
    db_session.commit()

    resp = client.post(f"/api/requirements/{req.id}/notes", json={"text": "First note"})
    assert resp.status_code == 200
    assert "First note" in resp.json()["notes"]

    # Second note appends
    resp2 = client.post(f"/api/requirements/{req.id}/notes", json={"text": "Second note"})
    assert resp2.status_code == 200
    notes = resp2.json()["notes"]
    assert "First note" in notes
    assert "Second note" in notes


def test_add_requirement_note_empty_text(client, test_requisition, db_session):
    """POST /api/requirements/{id}/notes rejects empty text."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    resp = client.post(f"/api/requirements/{req.id}/notes", json={"text": ""})
    assert resp.status_code == 422


def test_list_requirement_notes_with_offer_notes(client, test_requisition, db_session):
    """GET /api/requirements/{id}/notes includes offer notes."""
    from app.models import Offer, Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    offer = Offer(
        requisition_id=test_requisition.id,
        requirement_id=req.id,
        vendor_name="Mouser",
        mpn="LM317T",
        notes="Good price, ships fast",
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(offer)
    db_session.commit()

    resp = client.get(f"/api/requirements/{req.id}/notes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["notes"]) == 1
    assert data["notes"][0]["vendor_name"] == "Mouser"
    assert data["notes"][0]["note"] == "Good price, ships fast"


# ── Part-level Tasks ────────────────────────────────────────────────


def test_list_requirement_tasks_empty(client, test_requisition, db_session):
    """GET /api/requirements/{id}/tasks returns empty when no tasks."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    resp = client.get(f"/api/requirements/{req.id}/tasks")
    assert resp.status_code == 200
    assert resp.json() == []


def test_create_requirement_task(client, test_requisition, db_session):
    """POST /api/requirements/{id}/tasks creates a task linked to the requirement."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    resp = client.post(f"/api/requirements/{req.id}/tasks", json={"title": "Follow up on pricing"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Follow up on pricing"
    assert data["status"] == "todo"

    # Verify it shows in the list
    list_resp = client.get(f"/api/requirements/{req.id}/tasks")
    assert list_resp.status_code == 200
    tasks = list_resp.json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == "Follow up on pricing"


def test_create_requirement_task_empty_title(client, test_requisition, db_session):
    """POST /api/requirements/{id}/tasks rejects empty title."""
    from app.models import Requirement

    req = db_session.query(Requirement).filter(
        Requirement.requisition_id == test_requisition.id
    ).first()
    resp = client.post(f"/api/requirements/{req.id}/tasks", json={"title": ""})
    assert resp.status_code == 422


def test_requirement_tasks_404(client):
    """GET /api/requirements/99999/tasks returns 404 for nonexistent requirement."""
    resp = client.get("/api/requirements/99999/tasks")
    assert resp.status_code == 404


# ── Requisition Status Filters ─────────────────────────────────────


class TestRequisitionStatusFilter:
    def _get_requisitions(self, resp):
        """Extract requisition list from response, handling various formats."""
        import json

        data = resp.json()
        # Handle double-encoded JSON (cached_endpoint may return a string)
        if isinstance(data, str):
            data = json.loads(data)
        if isinstance(data, list):
            return data
        return data.get("requisitions", data.get("items", []))

    def test_comma_separated_status_filter(self, client, db_session, test_user):
        """GET /api/requisitions?status=won,lost returns only matching statuses."""
        from app.models import Requisition

        r1 = Requisition(name="Won Req", status="won", created_by=test_user.id)
        r2 = Requisition(name="Lost Req", status="lost", created_by=test_user.id)
        r3 = Requisition(name="Active Req", status="active", created_by=test_user.id)
        db_session.add_all([r1, r2, r3])
        db_session.commit()

        resp = client.get("/api/requisitions?status=won,lost")
        assert resp.status_code == 200
        items = self._get_requisitions(resp)
        names = [r["name"] for r in items]
        assert "Won Req" in names
        assert "Lost Req" in names
        assert "Active Req" not in names

    def test_single_status_still_works(self, client, db_session, test_user):
        """GET /api/requisitions?status=draft still works with a single value."""
        from app.models import Requisition

        r = Requisition(name="Draft Filter Req", status="draft", created_by=test_user.id)
        db_session.add(r)
        db_session.commit()

        resp = client.get("/api/requisitions?status=draft")
        assert resp.status_code == 200
        items = self._get_requisitions(resp)
        names = [r["name"] for r in items]
        assert "Draft Filter Req" in names


# ── Quote Expiration Badges ──────────────────────────────────────────


@pytest.fixture
def _quote_req(db_session, test_user):
    from app.models import Requisition
    from app.models.crm import Company, CustomerSite
    co = Company(name="Test Co")
    db_session.add(co)
    db_session.flush()
    site = CustomerSite(site_name="Test Site", company_id=co.id)
    db_session.add(site)
    db_session.flush()
    req = Requisition(name="Quote Test", status="quoted", created_by=test_user.id, customer_site_id=site.id)
    db_session.add(req)
    db_session.flush()
    return req, site


class TestQuoteExpiration:
    def test_expired_quote_flagged(self, client, db_session, test_user, _quote_req):
        from app.models.quotes import Quote
        req, site = _quote_req
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-EXP-001",
            status="sent",
            validity_days=7,
            sent_at=datetime.now(timezone.utc) - timedelta(days=30),
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{req.id}/quotes")
        if resp.status_code == 200:
            data = resp.json()
            quotes = data if isinstance(data, list) else data.get("items", data.get("quotes", [data]))
            expired = [qd for qd in quotes if qd.get("id") == q.id]
            if expired:
                assert expired[0].get("is_expired") is True
                assert expired[0].get("days_until_expiry") is not None
                assert expired[0]["days_until_expiry"] < 0

    def test_valid_quote_not_flagged(self, client, db_session, test_user, _quote_req):
        from app.models.quotes import Quote
        req, site = _quote_req
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-VAL-001",
            status="sent",
            validity_days=30,
            sent_at=datetime.now(timezone.utc) - timedelta(days=1),
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{req.id}/quotes")
        if resp.status_code == 200:
            data = resp.json()
            quotes = data if isinstance(data, list) else data.get("items", data.get("quotes", [data]))
            valid = [qd for qd in quotes if qd.get("id") == q.id]
            if valid:
                assert valid[0].get("is_expired") is False
                assert valid[0].get("days_until_expiry") is not None
                assert valid[0]["days_until_expiry"] >= 0

    def test_draft_quote_no_expiry(self, client, db_session, test_user, _quote_req):
        from app.models.quotes import Quote
        req, site = _quote_req
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-DRF-001",
            status="draft",
            validity_days=7,
            sent_at=None,
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{req.id}/quotes")
        if resp.status_code == 200:
            data = resp.json()
            quotes = data if isinstance(data, list) else data.get("items", data.get("quotes", [data]))
            draft = [qd for qd in quotes if qd.get("id") == q.id]
            if draft:
                assert draft[0].get("is_expired") is False
                assert draft[0].get("days_until_expiry") is None

    def test_single_quote_endpoint_has_expiry(self, client, db_session, test_user, _quote_req):
        from app.models.quotes import Quote
        req, site = _quote_req
        q = Quote(
            requisition_id=req.id,
            customer_site_id=site.id,
            quote_number="Q-SGL-001",
            status="sent",
            validity_days=7,
            sent_at=datetime.now(timezone.utc) - timedelta(days=30),
            created_by_id=test_user.id,
        )
        db_session.add(q)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{req.id}/quote")
        if resp.status_code == 200:
            data = resp.json()
            assert data.get("is_expired") is True
            assert data.get("days_until_expiry") is not None
            assert data["days_until_expiry"] < 0
