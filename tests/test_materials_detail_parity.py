"""Guards that the materials detail + tabs still render the same history after
refactoring them onto part_history_service helpers.

Called by: pytest (regression guard for Task 5 refactor).
Depends on: the /v2/partials/materials/{card_id} and .../tab/{tab_name} routes.
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.crm import Company
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.purchase_history import CustomerPartHistory
from app.models.sourcing import Requirement, Requisition, Sighting


def _seed(db: Session) -> MaterialCard:
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db.add(card)
    db.commit()
    db.refresh(card)
    req = Requisition(name="R", customer_name="ACME", status="active")
    db.add(req)
    db.commit()
    db.refresh(req)
    db.add(
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
    requirement = Requirement(
        requisition_id=req.id, primary_mpn="LM317T", material_card_id=card.id, sourcing_status="open"
    )
    db.add(requirement)
    db.commit()
    db.refresh(requirement)
    db.add(
        Sighting(
            requirement_id=requirement.id,
            material_card_id=card.id,
            vendor_name="TTI",
            qty_available=5,
            unit_price=Decimal("4.3"),
            source_type="brokerbin",
        )
    )
    co = Company(name="ACME Inc")
    db.add(co)
    db.commit()
    db.refresh(co)
    db.add(
        CustomerPartHistory(
            company_id=co.id,
            material_card_id=card.id,
            mpn="LM317T",
            source="acctivate_po",
            purchase_count=2,
            total_quantity=500,
            avg_unit_price=Decimal("3.90"),
        )
    )
    db.commit()
    return card


def test_material_detail_renders_card(client: TestClient, db_session: Session):
    """Detail page still loads and renders the part MPN after the refactor."""
    card = _seed(db_session)
    resp = client.get(f"/v2/partials/materials/{card.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "LM317T" in resp.text


def test_material_sourcing_tab_renders_requirement(client: TestClient, db_session: Session):
    card = _seed(db_session)
    resp = client.get(f"/v2/partials/materials/{card.id}/tab/sourcing", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "LM317T" in resp.text


def test_material_customers_tab_renders_purchase(client: TestClient, db_session: Session):
    card = _seed(db_session)
    resp = client.get(f"/v2/partials/materials/{card.id}/tab/customers", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "ACME Inc" in resp.text
