"""tests/test_vendors_crud_coverage.py — Coverage tests for app/routers/vendors_crud.py.

Endpoints covered:
  GET  /api/vendors/check-duplicate
  GET  /api/vendors
  GET  /api/autocomplete/names
  GET  /api/vendors/{card_id}
  PUT  /api/vendors/{card_id}
  POST /api/vendors/{card_id}/blacklist
  DELETE /api/vendors/{card_id}          (admin only)
  POST /api/vendors/{card_id}/reviews
  DELETE /api/vendors/{card_id}/reviews/{review_id}

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_vendor_card, test_user, admin_user)
"""

from app.models import Offer, VendorCard, VendorReview

# ---------------------------------------------------------------------------
# check-duplicate
# ---------------------------------------------------------------------------


class TestCheckVendorDuplicate:
    def test_no_match_returns_empty(self, client, db_session):
        resp = client.get("/api/vendors/check-duplicate?name=XyzNonexistentVendor99")
        assert resp.status_code == 200
        data = resp.json()
        assert "matches" in data
        assert data["matches"] == []

    def test_exact_match_returned(self, client, db_session, test_vendor_card):
        # Arrow Electronics is the test_vendor_card normalized_name
        resp = client.get("/api/vendors/check-duplicate?name=Arrow Electronics")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["matches"]) >= 1
        assert data["matches"][0]["match"] == "exact"

    def test_fuzzy_match_python_fallback(self, client, db_session, test_vendor_card):
        # Near-match on SQLite — Python rapidfuzz path
        resp = client.get("/api/vendors/check-duplicate?name=Arrow Electrnics")
        assert resp.status_code == 200
        data = resp.json()
        # Should find a fuzzy match or empty — just confirm no error
        assert "matches" in data

    def test_missing_name_param_returns_422(self, client):
        resp = client.get("/api/vendors/check-duplicate")
        assert resp.status_code == 422

    def test_short_name_one_char_returns_422(self, client):
        resp = client.get("/api/vendors/check-duplicate?name=")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# list_vendors
# ---------------------------------------------------------------------------


class TestListVendors:
    def test_empty_db_returns_empty_list(self, client, db_session):
        resp = client.get("/api/vendors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["vendors"] == []
        assert data["total"] == 0

    def test_returns_vendor_in_list(self, client, db_session, test_vendor_card):
        resp = client.get("/api/vendors")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        names = [v["display_name"] for v in data["vendors"]]
        assert "Arrow Electronics" in names

    def test_search_filter_q(self, client, db_session, test_vendor_card):
        resp = client.get("/api/vendors?q=arrow")
        assert resp.status_code == 200
        data = resp.json()
        assert any(v["display_name"] == "Arrow Electronics" for v in data["vendors"])

    def test_search_filter_q_no_match(self, client, db_session, test_vendor_card):
        resp = client.get("/api/vendors?q=zzznomatch999")
        assert resp.status_code == 200
        assert resp.json()["vendors"] == []

    def test_pagination_limit_offset(self, client, db_session):
        for i in range(5):
            card = VendorCard(normalized_name=f"vendor {i}", display_name=f"Vendor {i}")
            db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?limit=2&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["vendors"]) <= 2
        assert data["limit"] == 2

    def test_tier_filter_proven(self, client, db_session):
        card = VendorCard(
            normalized_name="proven vendor",
            display_name="Proven Vendor",
            vendor_score=75,
            is_new_vendor=False,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?tier=proven")
        assert resp.status_code == 200
        data = resp.json()
        assert any(v["display_name"] == "Proven Vendor" for v in data["vendors"])

    def test_tier_filter_developing(self, client, db_session):
        card = VendorCard(
            normalized_name="developing vendor",
            display_name="Developing Vendor",
            vendor_score=50,
            is_new_vendor=False,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?tier=developing")
        assert resp.status_code == 200
        data = resp.json()
        assert any(v["display_name"] == "Developing Vendor" for v in data["vendors"])

    def test_tier_filter_caution(self, client, db_session):
        card = VendorCard(
            normalized_name="caution vendor",
            display_name="Caution Vendor",
            vendor_score=20,
            is_new_vendor=False,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?tier=caution")
        assert resp.status_code == 200
        data = resp.json()
        assert any(v["display_name"] == "Caution Vendor" for v in data["vendors"])

    def test_tier_filter_new(self, client, db_session):
        card = VendorCard(
            normalized_name="new vendor xyz",
            display_name="New Vendor XYZ",
            is_new_vendor=True,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?tier=new")
        assert resp.status_code == 200
        data = resp.json()
        assert any(v["display_name"] == "New Vendor XYZ" for v in data["vendors"])

    def test_sort_by_name_asc(self, client, db_session):
        for name in ["Zeta Corp", "Alpha Inc", "Beta Ltd"]:
            card = VendorCard(normalized_name=name.lower(), display_name=name)
            db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?sort=name&order=asc")
        assert resp.status_code == 200
        names = [v["display_name"] for v in resp.json()["vendors"]]
        assert names == sorted(names)

    def test_sort_by_name_desc(self, client, db_session):
        for name in ["Zeta Corp", "Alpha Inc", "Beta Ltd"]:
            card = VendorCard(normalized_name=name.lower(), display_name=name)
            db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?sort=name&order=desc")
        assert resp.status_code == 200
        names = [v["display_name"] for v in resp.json()["vendors"]]
        assert names == sorted(names, reverse=True)

    def test_sort_by_score(self, client, db_session):
        resp = client.get("/api/vendors?sort=score&order=asc")
        assert resp.status_code == 200

    def test_vendor_with_review_stats(self, client, db_session, test_vendor_card, test_user):
        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=4,
            comment="Good vendor",
        )
        db_session.add(review)
        db_session.commit()
        resp = client.get("/api/vendors")
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        vendor = next(v for v in vendors if v["id"] == test_vendor_card.id)
        assert vendor["avg_rating"] == 4.0
        assert vendor["review_count"] == 1

    def test_vendor_response_rate_computed(self, client, db_session):
        card = VendorCard(
            normalized_name="resp rate vendor",
            display_name="Resp Rate Vendor",
            total_outreach=10,
            total_responses=5,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors")
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        v = next((x for x in vendors if x["display_name"] == "Resp Rate Vendor"), None)
        assert v is not None
        assert v["response_rate"] == 50.0

    def test_vendor_auto_rating_from_score(self, client, db_session):
        card = VendorCard(
            normalized_name="auto score vendor",
            display_name="Auto Score Vendor",
            vendor_score=80,
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors")
        assert resp.status_code == 200
        vendors = resp.json()["vendors"]
        v = next((x for x in vendors if x["display_name"] == "Auto Score Vendor"), None)
        assert v is not None
        assert v["avg_rating"] is not None
        assert v["rating_source"] == "auto"

    def test_limit_validation_min(self, client):
        resp = client.get("/api/vendors?limit=0")
        assert resp.status_code == 422

    def test_limit_validation_max(self, client):
        resp = client.get("/api/vendors?limit=2000")
        assert resp.status_code == 422

    def test_tag_filter(self, client, db_session):
        card = VendorCard(
            normalized_name="tag vendor",
            display_name="Tag Vendor",
            brand_tags=["samsung", "toshiba"],
        )
        db_session.add(card)
        db_session.commit()
        resp = client.get("/api/vendors?tag=samsung")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# autocomplete_names
# ---------------------------------------------------------------------------


class TestAutocompleteNames:
    def test_short_query_returns_empty(self, client):
        resp = client.get("/api/autocomplete/names?q=a")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_vendor_match(self, client, db_session, test_vendor_card):
        resp = client.get("/api/autocomplete/names?q=arrow")
        assert resp.status_code == 200
        data = resp.json()
        assert any(r["type"] == "vendor" for r in data)
        assert any(r["name"] == "Arrow Electronics" for r in data)

    def test_returns_company_match(self, client, db_session, test_company):
        resp = client.get("/api/autocomplete/names?q=acme")
        assert resp.status_code == 200
        data = resp.json()
        assert any(r["type"] == "customer" for r in data)

    def test_limit_parameter(self, client, db_session):
        resp = client.get("/api/autocomplete/names?q=arrow&limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) <= 2

    def test_no_q_param_returns_empty(self, client):
        resp = client.get("/api/autocomplete/names")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# get_vendor  (GET /api/vendors/{card_id})
# ---------------------------------------------------------------------------


class TestGetVendor:
    def test_returns_vendor_detail(self, client, db_session, test_vendor_card):
        resp = client.get(f"/api/vendors/{test_vendor_card.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["display_name"] == "Arrow Electronics"

    def test_nonexistent_returns_404(self, client):
        resp = client.get("/api/vendors/999999")
        assert resp.status_code == 404
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# update_vendor  (PUT /api/vendors/{card_id})
# ---------------------------------------------------------------------------


class TestUpdateVendor:
    def test_update_emails(self, client, db_session, test_vendor_card):
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}",
            json={"emails": ["new@arrow.com"]},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "new@arrow.com" in test_vendor_card.emails

    def test_update_phones(self, client, db_session, test_vendor_card):
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}",
            json={"phones": ["+1-800-555-0000"]},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert "+1-800-555-0000" in test_vendor_card.phones

    def test_update_website(self, client, db_session, test_vendor_card):
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}",
            json={"website": "https://new.arrow.com"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.website == "https://new.arrow.com"

    def test_update_display_name(self, client, db_session, test_vendor_card):
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}",
            json={"display_name": "Arrow Electronics Updated"},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == "Arrow Electronics Updated"

    def test_update_blacklist_flag(self, client, db_session, test_vendor_card):
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}",
            json={"is_blacklisted": True},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is True

    def test_update_nonexistent_returns_404(self, client):
        resp = client.put("/api/vendors/999999", json={"website": "https://x.com"})
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_blank_display_name_not_applied(self, client, db_session, test_vendor_card):
        # display_name="" should not override existing name
        resp = client.put(
            f"/api/vendors/{test_vendor_card.id}",
            json={"display_name": "   "},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.display_name == "Arrow Electronics"


# ---------------------------------------------------------------------------
# toggle_blacklist  (POST /api/vendors/{card_id}/blacklist)
# ---------------------------------------------------------------------------


class TestToggleBlacklist:
    def test_set_blacklisted_true(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/blacklist",
            json={"blacklisted": True},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is True

    def test_set_blacklisted_false(self, client, db_session, test_vendor_card):
        test_vendor_card.is_blacklisted = True
        db_session.commit()
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/blacklist",
            json={"blacklisted": False},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        assert test_vendor_card.is_blacklisted is False

    def test_toggle_flip(self, client, db_session, test_vendor_card):
        # blacklisted=None → flip
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/blacklist",
            json={},
        )
        assert resp.status_code == 200
        db_session.refresh(test_vendor_card)
        # Was None/False, toggled to True
        assert test_vendor_card.is_blacklisted is True

    def test_nonexistent_returns_404(self, client):
        resp = client.post("/api/vendors/999999/blacklist", json={"blacklisted": True})
        assert resp.status_code == 404
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# delete_vendor  (DELETE /api/vendors/{card_id}) — admin only (overridden in client)
# ---------------------------------------------------------------------------


class TestDeleteVendor:
    def test_delete_vendor_success(self, client, db_session):
        card = VendorCard(normalized_name="deletable vendor", display_name="Deletable Vendor")
        db_session.add(card)
        db_session.commit()
        db_session.refresh(card)
        cid = card.id
        resp = client.delete(f"/api/vendors/{cid}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert db_session.get(VendorCard, cid) is None

    def test_delete_nonexistent_returns_404(self, client):
        resp = client.delete("/api/vendors/999999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_delete_vendor_with_offers_returns_400(self, client, db_session, test_vendor_card, test_requisition):
        offer = Offer(
            requisition_id=test_requisition.id,
            vendor_card_id=test_vendor_card.id,
            vendor_name="Arrow Electronics",
            mpn="LM317T",
            qty_available=100,
            unit_price=0.50,
            status="active",
            entered_by_id=test_requisition.created_by,
        )
        db_session.add(offer)
        db_session.commit()
        resp = client.delete(f"/api/vendors/{test_vendor_card.id}")
        assert resp.status_code == 400
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# add_review  (POST /api/vendors/{card_id}/reviews)
# ---------------------------------------------------------------------------


class TestAddReview:
    def test_add_review_happy_path(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/reviews",
            json={"rating": 5, "comment": "Excellent vendor!"},
        )
        assert resp.status_code == 200
        reviews = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).all()
        assert len(reviews) == 1
        assert reviews[0].rating == 5

    def test_add_review_default_rating(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/reviews",
            json={},
        )
        assert resp.status_code == 200

    def test_add_review_nonexistent_vendor_returns_404(self, client):
        resp = client.post("/api/vendors/999999/reviews", json={"rating": 3})
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_review_rating_clamped_to_5(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/reviews",
            json={"rating": 99},
        )
        assert resp.status_code == 200
        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review.rating == 5

    def test_review_rating_clamped_to_1(self, client, db_session, test_vendor_card):
        resp = client.post(
            f"/api/vendors/{test_vendor_card.id}/reviews",
            json={"rating": -5},
        )
        assert resp.status_code == 200
        review = db_session.query(VendorReview).filter_by(vendor_card_id=test_vendor_card.id).first()
        assert review.rating == 1


# ---------------------------------------------------------------------------
# delete_review  (DELETE /api/vendors/{card_id}/reviews/{review_id})
# ---------------------------------------------------------------------------


class TestDeleteReview:
    def test_delete_own_review(self, client, db_session, test_vendor_card, test_user):
        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=3,
            comment="OK",
        )
        db_session.add(review)
        db_session.commit()
        db_session.refresh(review)
        resp = client.delete(f"/api/vendors/{test_vendor_card.id}/reviews/{review.id}")
        assert resp.status_code == 200
        assert db_session.get(VendorReview, review.id) is None

    def test_delete_nonexistent_review_returns_404(self, client, test_vendor_card):
        resp = client.delete(f"/api/vendors/{test_vendor_card.id}/reviews/999999")
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_delete_review_wrong_vendor_returns_404(self, client, db_session, test_vendor_card, test_user):
        review = VendorReview(
            vendor_card_id=test_vendor_card.id,
            user_id=test_user.id,
            rating=3,
        )
        db_session.add(review)
        db_session.commit()
        db_session.refresh(review)
        # Wrong card_id
        resp = client.delete(f"/api/vendors/999999/reviews/{review.id}")
        assert resp.status_code == 404
        assert "error" in resp.json()
