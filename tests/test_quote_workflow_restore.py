"""
test_quote_workflow_restore.py — Tests for the restored quote workflow.

Tests the new quote-summary endpoint, requisition-to-buy-plan bridge,
risk flag model, input sanitization, and transaction safety.

Called by: pytest
Depends on: conftest.py (client, db_session, test_user fixtures)
"""

from app.models.risk_flag import RiskFlag, RiskFlagSeverity, RiskFlagType
from app.utils.sanitize import sanitize_dict, sanitize_text


# ── Helpers ──────────────────────────────────────────────────────────────


def _setup_req_with_offers(client):
    """Create a requisition linked to a customer site with offers."""
    co = client.post("/api/companies", json={"name": "Workflow Test Corp"}).json()
    site = client.post(
        f"/api/companies/{co['id']}/sites",
        json={"site_name": "HQ", "contact_name": "Jane", "contact_email": "jane@test.com"},
    ).json()
    req = client.post(
        "/api/requisitions",
        json={"name": "Workflow Restore Test", "customer_site_id": site["id"]},
    ).json()
    req_id = req["id"]
    items = client.post(
        f"/api/requisitions/{req_id}/requirements",
        json=[{"primary_mpn": "LM317T", "target_qty": 500, "target_price": 0.50}],
    ).json()["created"]
    offer = client.post(
        f"/api/requisitions/{req_id}/offers",
        json={
            "mpn": "LM317T",
            "vendor_name": "Arrow Electronics",
            "unit_price": 0.45,
            "qty_available": 1000,
            "requirement_id": items[0]["id"],
        },
    ).json()
    return req_id, [offer["id"]]


# ── Quote Summary Endpoint ───────────────────────────────────────────────


class TestQuoteSummary:
    """GET /api/requisitions/{id}/quote-summary — lightweight quote tab projection."""

    def test_summary_no_quote(self, client):
        """Summary with no quote returns actionable empty state."""
        co = client.post("/api/companies", json={"name": "Summary Corp"}).json()
        site = client.post(
            f"/api/companies/{co['id']}/sites",
            json={"site_name": "HQ", "contact_name": "J", "contact_email": "j@t.com"},
        ).json()
        req = client.post(
            "/api/requisitions",
            json={"name": "No Quote Req", "customer_site_id": site["id"]},
        ).json()
        resp = client.get(f"/api/requisitions/{req['id']}/quote-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_quote"] is False
        assert data["has_buy_plan"] is False
        assert data["requisition_id"] == req["id"]

    def test_summary_with_quote(self, client):
        """Summary with a quote returns quote metadata."""
        req_id, offer_ids = _setup_req_with_offers(client)
        client.post(f"/api/requisitions/{req_id}/quote", json={"offer_ids": offer_ids})
        resp = client.get(f"/api/requisitions/{req_id}/quote-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_quote"] is True
        assert data["quote_status"] == "draft"
        assert data["line_count"] >= 1
        assert "quote_number" in data

    def test_summary_nonexistent_req(self, client):
        resp = client.get("/api/requisitions/99999/quote-summary")
        assert resp.status_code == 404


# ── Buy Plan Bridge ─────────────────────────────────────────────────────


class TestBuyPlanBridge:
    """POST /api/requisitions/{id}/buy-plan — creates or returns buy plan."""

    def test_no_quote_returns_400(self, client):
        """Cannot create buy plan without a quote."""
        co = client.post("/api/companies", json={"name": "BP Corp"}).json()
        site = client.post(
            f"/api/companies/{co['id']}/sites",
            json={"site_name": "HQ", "contact_name": "J", "contact_email": "j@t.com"},
        ).json()
        req = client.post(
            "/api/requisitions",
            json={"name": "No Quote BP", "customer_site_id": site["id"]},
        ).json()
        resp = client.post(f"/api/requisitions/{req['id']}/buy-plan")
        assert resp.status_code == 400

    def test_nonexistent_req(self, client):
        resp = client.post("/api/requisitions/99999/buy-plan")
        assert resp.status_code == 404


# ── Transaction Safety ──────────────────────────────────────────────────


class TestQuoteTransactionSafety:
    """Quote creation wraps operations in transaction boundaries."""

    def test_create_quote_success(self, client):
        """Quote creation succeeds and returns valid data."""
        req_id, offer_ids = _setup_req_with_offers(client)
        resp = client.post(
            f"/api/requisitions/{req_id}/quote",
            json={"offer_ids": offer_ids},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data
        assert data["status"] == "draft"

    def test_create_quote_no_site_fails(self, client):
        """Quote creation without customer site returns 400, not 500."""
        req = client.post("/api/requisitions", json={"name": "No Site"}).json()
        resp = client.post(
            f"/api/requisitions/{req['id']}/quote",
            json={"offer_ids": []},
        )
        assert resp.status_code == 400

    def test_subsequent_reads_after_failed_quote(self, client):
        """After a failed quote attempt, reads still work (no poisoned session)."""
        req = client.post("/api/requisitions", json={"name": "Fail Test"}).json()
        # Try to create quote without site (should fail with 400)
        client.post(f"/api/requisitions/{req['id']}/quote", json={"offer_ids": []})
        # Subsequent read should still work
        resp = client.get(f"/api/requisitions/{req['id']}/quote-summary")
        assert resp.status_code == 200


# ── Risk Flag Model ─────────────────────────────────────────────────────


class TestRiskFlagModel:
    """Risk flag model and enum validation."""

    def test_risk_flag_types(self):
        """All expected risk flag types are defined."""
        expected = {
            "price_increase", "lead_time_risk", "vendor_reliability",
            "qty_shortfall", "geo_risk", "stale_offer",
            "margin_below_threshold", "single_source", "counterfeit_risk", "other",
        }
        actual = {t.value for t in RiskFlagType}
        assert expected == actual

    def test_risk_flag_severities(self):
        assert {s.value for s in RiskFlagSeverity} == {"info", "warning", "critical"}

    def test_create_risk_flag(self, db_session, test_user):
        """Risk flag can be created and persisted."""
        from app.models import Requisition

        req = Requisition(name="RF Test", status="active", created_by=test_user.id)
        db_session.add(req)
        db_session.flush()

        flag = RiskFlag(
            requisition_id=req.id,
            type=RiskFlagType.stale_offer.value,
            severity=RiskFlagSeverity.warning.value,
            message="Offer is older than 14 days",
            source="rule",
        )
        db_session.add(flag)
        db_session.commit()
        assert flag.id is not None
        assert flag.type == "stale_offer"
        assert flag.severity == "warning"


# ── Risk Flags in Offer Response ─────────────────────────────────────────


class TestOfferRiskFlags:
    """Risk flags are surfaced in the offer listing response."""

    def test_offers_include_risk_flags(self, client, db_session):
        """Offer response includes risk_flags array."""
        req_id, offer_ids = _setup_req_with_offers(client)
        # Create a risk flag for the offer
        flag = RiskFlag(
            source_offer_id=offer_ids[0],
            requisition_id=req_id,
            type="stale_offer",
            severity="warning",
            message="Test risk flag",
        )
        db_session.add(flag)
        db_session.commit()

        resp = client.get(f"/api/requisitions/{req_id}/offers")
        assert resp.status_code == 200
        data = resp.json()
        groups = data.get("groups", [])
        found_flag = False
        for g in groups:
            for o in g.get("offers", []):
                if o["id"] == offer_ids[0]:
                    assert len(o.get("risk_flags", [])) >= 1
                    assert o["risk_flags"][0]["type"] == "stale_offer"
                    found_flag = True
        assert found_flag, "Risk flag not found in offer response"


# ── Input Sanitization ──────────────────────────────────────────────────


class TestSanitization:
    """Input sanitization prevents stored XSS."""

    def test_sanitize_strips_script_tags(self):
        assert "<script>" not in sanitize_text("<script>alert('xss')</script>")

    def test_sanitize_strips_event_handlers(self):
        result = sanitize_text('<img onerror="alert(1)">')
        assert "onerror=" not in result

    def test_sanitize_strips_javascript_uri(self):
        result = sanitize_text('javascript:alert(1)')
        assert "javascript:" not in result

    def test_sanitize_preserves_normal_text(self):
        assert sanitize_text("LM317T voltage regulator") == "LM317T voltage regulator"

    def test_sanitize_none_returns_none(self):
        assert sanitize_text(None) is None

    def test_sanitize_dict_fields(self):
        data = {"name": "<script>bad</script>", "price": 1.5, "notes": "safe text"}
        result = sanitize_dict(data, ["name", "notes", "missing_field"])
        assert "<script>" not in result["name"]
        assert result["notes"] == "safe text"
        assert result["price"] == 1.5

    def test_xss_in_offer_creation(self, client):
        """XSS payload in offer vendor_name is sanitized."""
        req_id, _ = _setup_req_with_offers(client)
        resp = client.post(
            f"/api/requisitions/{req_id}/offers",
            json={
                "mpn": "TEST-MPN",
                "vendor_name": "<script>alert('xss')</script>Evil Corp",
                "unit_price": 1.0,
                "qty_available": 100,
            },
        )
        assert resp.status_code == 200
        assert "<script>" not in resp.json().get("vendor_name", "")
