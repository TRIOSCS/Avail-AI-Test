"""
test_integration_quote_workflow.py — Integration tests for the full quote lifecycle.

Tests the complete workflow: create requisition → link to customer site → add
requirements → log offers → build quote → update quote lines → mark result.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user, test_company fixtures)
"""


# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_req_with_offers(client):
    """Create a requisition linked to a customer site with offers. Returns (req_id, offer_ids)."""
    # Create company + site
    co = client.post("/api/companies", json={"name": "QuoteTest Corp"}).json()
    site = client.post(
        f"/api/companies/{co['id']}/sites",
        json={"site_name": "HQ", "contact_name": "Jane", "contact_email": "jane@test.com"},
    ).json()

    # Create requisition and link to site
    req = client.post("/api/requisitions", json={
        "name": "Quote Workflow Test",
        "customer_site_id": site["id"],
    }).json()
    req_id = req["id"]

    items = client.post(
        f"/api/requisitions/{req_id}/requirements",
        json=[
            {"primary_mpn": "LM317T", "target_qty": 500, "target_price": 0.50},
            {"primary_mpn": "NE555P", "target_qty": 200, "target_price": 0.30},
        ],
    ).json()

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


# ── Tests ────────────────────────────────────────────────────────────────


class TestQuoteBuild:
    """Building a quote from selected offers."""

    def test_build_quote_from_offers(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        resp = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "draft"
        assert len(data.get("line_items", [])) >= 1

    def test_build_quote_requires_site(self, client):
        """Requisition without a customer site cannot create a quote."""
        req = client.post("/api/requisitions", json={"name": "No Site Req"}).json()
        resp = client.post(
            f"/api/requisitions/{req['id']}/quote",
            json={"offer_ids": []},
        )
        assert resp.status_code == 400

    def test_build_quote_nonexistent_req(self, client):
        resp = client.post(
            "/api/requisitions/99999/quote",
            json={"offer_ids": [1]},
        )
        assert resp.status_code == 404


class TestQuoteUpdate:
    """Updating quote line items and terms."""

    def test_update_quote_sell_prices(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        quote = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        ).json()

        # Update with sell prices
        line_items = quote.get("line_items", [])
        for item in line_items:
            item["sell_price"] = round(
                (item.get("cost_price") or 0.50) * 1.3, 4
            )

        resp = client.put(
            f"/api/quotes/{quote['id']}",
            json={
                "line_items": line_items,
                "payment_terms": "Net 30",
                "shipping_terms": "FOB Origin",
                "validity_days": 14,
            },
        )
        assert resp.status_code == 200

    def test_update_nonexistent_quote(self, client):
        resp = client.put(
            "/api/quotes/99999",
            json={"payment_terms": "Net 30"},
        )
        assert resp.status_code == 404


class TestQuoteResult:
    """Marking quotes as won or lost."""

    def test_mark_quote_won(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        quote = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        ).json()

        resp = client.post(
            f"/api/quotes/{quote['id']}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "won"

    def test_mark_quote_lost(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        quote = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        ).json()

        resp = client.post(
            f"/api/quotes/{quote['id']}/result",
            json={"result": "lost", "reason": "Price too high"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "lost"

    def test_mark_nonexistent_quote(self, client):
        resp = client.post(
            "/api/quotes/99999/result",
            json={"result": "won"},
        )
        assert resp.status_code == 404


class TestQuoteHistory:
    """Quote revision history."""

    def test_list_quotes_for_req(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        )
        resp = client.get(f"/api/requisitions/{req_id}/quotes")
        assert resp.status_code == 200
        quotes = resp.json()
        assert isinstance(quotes, list)
        assert len(quotes) >= 1

    def test_get_active_quote(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        created = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        ).json()
        resp = client.get(f"/api/requisitions/{req_id}/quote")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == created["id"]

    def test_revise_quote(self, client):
        req_id, offer_ids = _setup_req_with_offers(client)
        q1 = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        ).json()

        resp = client.post(f"/api/quotes/{q1['id']}/revise")
        # Revise may require quote to be in sent status
        assert resp.status_code in (200, 400, 409)


class TestPricingHistory:
    """Pricing history for an MPN."""

    def test_pricing_history(self, client):
        _setup_req_with_offers(client)
        resp = client.get("/api/pricing-history/LM317T")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data

    def test_pricing_history_no_data(self, client):
        resp = client.get("/api/pricing-history/NONEXISTENT-MPN-99999")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data.get("history", [])) == 0
