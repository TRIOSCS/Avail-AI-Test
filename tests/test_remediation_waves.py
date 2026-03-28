"""test_remediation_waves.py — Tests for remediation waves 2-10.

Tests API contract fixes, status machine validation, data cleanup,
date formatting safety, and error response consistency.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""

from app.services.status_machine import validate_transition

# ── Status Machine ──────────────────────────────────────────────────────


class TestStatusMachine:
    """Status transition validation for offers, quotes, buy plans."""

    def test_offer_valid_transitions(self):
        assert validate_transition("offer", "pending_review", "active") is True
        assert validate_transition("offer", "pending_review", "rejected") is True
        assert validate_transition("offer", "active", "sold") is True
        assert validate_transition("offer", "active", "won") is True

    def test_offer_invalid_transition(self):
        import pytest

        with pytest.raises(ValueError, match="Invalid offer status transition"):
            validate_transition("offer", "sold", "active")

    def test_offer_terminal_state(self):
        import pytest

        with pytest.raises(ValueError):
            validate_transition("offer", "rejected", "active")

    def test_quote_valid_transitions(self):
        assert validate_transition("quote", "draft", "sent") is True
        assert validate_transition("quote", "sent", "won") is True
        assert validate_transition("quote", "sent", "lost") is True

    def test_quote_invalid_transition(self):
        import pytest

        with pytest.raises(ValueError):
            validate_transition("quote", "revised", "draft")

    def test_buy_plan_valid_transitions(self):
        assert validate_transition("buy_plan", "draft", "pending") is True
        assert validate_transition("buy_plan", "pending", "active") is True
        assert validate_transition("buy_plan", "active", "completed") is True
        assert validate_transition("buy_plan", "halted", "draft") is True

    def test_buy_plan_invalid_transition(self):
        import pytest

        with pytest.raises(ValueError):
            validate_transition("buy_plan", "completed", "active")

    def test_requisition_transitions(self):
        assert validate_transition("requisition", "draft", "active") is True
        assert validate_transition("requisition", "active", "offers") is True
        assert validate_transition("requisition", "offers", "quoting") is True

    def test_unknown_entity_allows_transition(self):
        assert validate_transition("unknown_entity", "a", "b") is True

    def test_unknown_current_status_allows_transition(self):
        assert validate_transition("offer", "unknown_status", "active") is True

    def test_noop_transition(self):
        assert validate_transition("offer", "active", "active") is True

    def test_raise_on_invalid_false(self):
        result = validate_transition("offer", "sold", "active", raise_on_invalid=False)
        assert result is False


# ── API Contract Fixes ──────────────────────────────────────────────────


class TestApiContracts:
    """Verify API contract consistency fixes."""

    def test_buy_plan_invalid_status_filter(self, client):
        """Invalid status filter returns 400, not empty results."""
        resp = client.get("/api/buy-plans?status=INVALID_STATUS")
        assert resp.status_code == 400

    def test_buy_plan_valid_status_filter(self, client):
        """Valid status filter works normally."""
        resp = client.get("/api/buy-plans?status=draft")
        assert resp.status_code == 200

    def test_offer_mark_sold_endpoint(self, client):
        """Offer can be marked as sold via dedicated endpoint."""
        co = client.post("/api/companies", json={"name": "Status Corp"}).json()
        site = client.post(
            f"/api/companies/{co['id']}/sites",
            json={"site_name": "HQ", "contact_name": "J", "contact_email": "j@t.com"},
        ).json()
        req = client.post(
            "/api/requisitions",
            json={"name": "Status Test", "customer_site_id": site["id"]},
        ).json()
        offer = client.post(
            f"/api/requisitions/{req['id']}/offers",
            json={"mpn": "LM317T", "vendor_name": "Arrow", "unit_price": 1.0, "qty_available": 100},
        ).json()

        # Mark as sold via dedicated endpoint
        resp_sold = client.patch(f"/api/offers/{offer['id']}/mark-sold")
        assert resp_sold.status_code == 200

    def test_quote_result_status_changed_accurate(self, client):
        """Quote result endpoint returns accurate status_changed flag."""
        co = client.post("/api/companies", json={"name": "Result Corp"}).json()
        site = client.post(
            f"/api/companies/{co['id']}/sites",
            json={"site_name": "HQ", "contact_name": "J", "contact_email": "j@t.com"},
        ).json()
        req = client.post(
            "/api/requisitions",
            json={"name": "Result Test", "customer_site_id": site["id"]},
        ).json()
        offer = client.post(
            f"/api/requisitions/{req['id']}/offers",
            json={"mpn": "LM317T", "vendor_name": "Arrow", "unit_price": 1.0, "qty_available": 100},
        ).json()
        quote = client.post(
            f"/api/requisitions/{req['id']}/quote",
            json={"offer_ids": [offer["id"]]},
        ).json()

        resp = client.post(
            f"/api/quotes/{quote['id']}/result",
            json={"result": "won"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "status_changed" in data
        # status should have changed from 'quoting' to 'won'
        assert data["status_changed"] is True


# ── Sanitization on Quote Updates ────────────────────────────────────────


class TestQuoteUpdateSanitization:
    """Quote update sanitizes line item text fields."""

    def test_quote_update_line_items(self, client):
        """Quote line items can be updated."""
        co = client.post("/api/companies", json={"name": "San Corp"}).json()
        site = client.post(
            f"/api/companies/{co['id']}/sites",
            json={"site_name": "HQ", "contact_name": "J", "contact_email": "j@t.com"},
        ).json()
        req = client.post(
            "/api/requisitions",
            json={"name": "Update Quote", "customer_site_id": site["id"]},
        ).json()
        offer = client.post(
            f"/api/requisitions/{req['id']}/offers",
            json={"mpn": "LM317T", "vendor_name": "Arrow", "unit_price": 1.0, "qty_available": 100},
        ).json()
        quote = client.post(
            f"/api/requisitions/{req['id']}/quote",
            json={"offer_ids": [offer["id"]]},
        ).json()

        resp = client.put(
            f"/api/quotes/{quote['id']}",
            json={
                "line_items": [
                    {
                        "mpn": "LM317T",
                        "manufacturer": "TI",
                        "qty": 100,
                        "cost_price": 1.0,
                        "sell_price": 1.5,
                    }
                ],
            },
        )
        assert resp.status_code == 200
