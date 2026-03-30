"""test_integration_quote_workflow.py — Integration tests for the full quote lifecycle.

Tests the complete workflow: create requisition → link to customer site → add
requirements → log offers → build quote → update quote lines → mark result.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_company fixtures)
"""

import pytest

pytestmark = pytest.mark.slow

# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_req_with_offers(client):
    """Create a requisition linked to a customer site with offers.

    Returns (req_id, offer_ids).
    """
    # Create company + site
    co = client.post("/api/companies", json={"name": "QuoteTest Corp"}).json()
    site = client.post(
        f"/api/companies/{co['id']}/sites",
        json={"site_name": "HQ", "contact_name": "Jane", "contact_email": "jane@test.com"},
    ).json()

    # Create requisition and link to site
    req = client.post(
        "/api/requisitions",
        json={
            "name": "Quote Workflow Test",
            "customer_site_id": site["id"],
        },
    ).json()
    req_id = req["id"]

    items = client.post(
        f"/api/requisitions/{req_id}/requirements",
        json=[
            {"primary_mpn": "LM317T", "manufacturer": "TI", "target_qty": 500, "target_price": 0.50},
            {"primary_mpn": "NE555P", "manufacturer": "TI", "target_qty": 200, "target_price": 0.30},
        ],
    ).json()["created"]

    offer1 = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Arrow Electronics",
            "unit_price": 0.45,
            "qty_available": 1000,
            "requirement_id": items[0]["id"],
        },
    ).json()

    offer2 = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "NE555P",
            "vendor_name": "Mouser",
            "unit_price": 0.25,
            "qty_available": 500,
            "requirement_id": items[1]["id"],
        },
    ).json()

    return req_id, [offer1["id"], offer2["id"]]
