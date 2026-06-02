"""Tests for GET /v2/partials/search/history — the search-page history panel.

Called by: pytest.
Depends on: part_history_service, the search_history_panel route, history_panel.html.
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requisition


def test_unknown_mpn_returns_empty_state(client: TestClient, db_session: Session):
    resp = client.get("/v2/partials/search/history?mpn=NOSUCHPART", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "looks new" in resp.text.lower()


def test_known_mpn_renders_history(client: TestClient, db_session: Session):
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    req = Requisition(name="R", customer_name="ACME", status="active")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    db_session.add(
        Offer(
            requisition_id=req.id,
            material_card_id=card.id,
            vendor_name="Avnet",
            mpn="LM317T",
            qty_available=10,
            unit_price=Decimal("4.1"),
            status="active",
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    # "LM-317T" normalizes to the same key "lm317t" as the stored card.
    resp = client.get("/v2/partials/search/history?mpn=LM-317T", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Avnet" in resp.text  # offer rendered
    assert f"/v2/materials/{card.id}" in resp.text  # deep link to full part page
