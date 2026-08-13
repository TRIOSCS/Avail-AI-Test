"""test_qcm_proactive_honesty.py — proactive send never lies about the outcome.

Two 2026-08-08 QC audit findings:
- send_proactive_offer swallows a Graph rejection (marks the offer FAILED) but the
  send route still rendered a "sent" success banner — a phantom success.
- _build_line_items honored a seeded 0 sell price, emailing a customer a $0.00
  unit price.

Called by: pytest
Depends on: app.services.proactive_service, app.routers.htmx.proactive, conftest
"""

from decimal import Decimal
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def _seeded_match(db: Session, mpn: str, unit_price: str):
    from app.models import Offer, ProactiveMatch, Requirement, Requisition, User

    user = User(email=f"{mpn}@x.com", name="R", role="sales", azure_id=f"az-{mpn}")
    db.add(user)
    db.flush()
    req = Requisition(name=f"rq-{mpn}", status="open", created_by=user.id)
    db.add(req)
    db.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn=mpn, target_qty=10)
    db.add(requirement)
    db.flush()
    offer = Offer(vendor_name="V", mpn=mpn, qty_available=100, unit_price=Decimal(unit_price), status="active")
    db.add(offer)
    db.flush()
    m = ProactiveMatch(offer_id=offer.id, requirement_id=requirement.id, salesperson_id=user.id, mpn=mpn, status="new")
    db.add(m)
    db.flush()
    m.offer = offer
    m.requirement = requirement
    return m


def test_build_line_items_skips_seeded_zero_price(db_session: Session):
    """A seeded 0 sell price must NOT email a $0.00 line, even with a cost basis."""
    from app.services.proactive_service import _build_line_items

    m = _seeded_match(db_session, "ZEROSEED", "8.00")
    lines, total_sell, _ = _build_line_items([m], {str(m.id): 0})
    assert lines == []
    assert total_sell == Decimal("0")


def test_build_line_items_keeps_positive_seeded_price(db_session: Session):
    from app.services.proactive_service import _build_line_items

    m = _seeded_match(db_session, "POSSEED", "8.00")
    lines, _, _ = _build_line_items([m], {str(m.id): 1.25})
    assert len(lines) == 1
    assert lines[0]["sell_price"] == 1.25


def test_send_surfaces_failure_not_phantom_success(client: TestClient, monkeypatch):
    """When the Graph send fails, the route raises (error toast), never a 'sent'
    banner."""
    from app.services import proactive_service

    async def _failed(**kwargs):
        return {"status": "failed", "line_items": [], "recipient_emails": []}

    monkeypatch.setattr(proactive_service, "send_proactive_offer", _failed)
    monkeypatch.setattr("app.scheduler.get_valid_token", AsyncMock(return_value="tok"))
    resp = client.post(
        "/v2/proactive/send",
        data={"match_ids": "1", "contact_ids": "1"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 502
    assert "sent" not in resp.text.lower() or "not delivered" in resp.text.lower()
