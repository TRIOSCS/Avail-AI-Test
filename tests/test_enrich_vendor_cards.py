"""test_enrich_vendor_cards.py — Tests for vendor_utils._enrich_with_vendor_cards.

Covers lines 216-356 of app/vendor_utils.py.

Called by: pytest
Depends on: app/vendor_utils, app/models/VendorCard, app/models/VendorReview
"""

import os
from datetime import UTC, datetime
from unittest.mock import patch

os.environ["TESTING"] = "1"


from app.models import VendorCard
from app.vendor_utils import _enrich_with_vendor_cards


def _make_card(db, normalized_name, display_name, **kwargs):
    card = VendorCard(
        normalized_name=normalized_name,
        display_name=display_name,
        emails=kwargs.get("emails", []),
        phones=kwargs.get("phones", []),
        sighting_count=kwargs.get("sighting_count", 0),
        is_blacklisted=kwargs.get("is_blacklisted", False),
        is_new_vendor=kwargs.get("is_new_vendor", True),
        vendor_score=kwargs.get("vendor_score", None),
        website=kwargs.get("website", None),
        created_at=datetime.now(UTC),
    )
    db.add(card)
    db.commit()
    return card


def _sighting(vendor_name, mpn="LM317T", **overrides):
    """Build a single fake sighting dict with sensible defaults."""
    sighting = {
        "vendor_name": vendor_name,
        "mpn_matched": mpn,
        "vendor_email": None,
        "vendor_phone": None,
        "vendor_url": None,
        "is_historical": False,
        "is_material_history": False,
    }
    sighting.update(overrides)
    return sighting


def _make_results(*vendor_names, mpn="LM317T", **overrides):
    """Build a fake search results dict with one sighting per vendor name (all under the
    given mpn).

    Per-sighting fields can be overridden via kwargs.
    """
    return {mpn: {"sightings": [_sighting(name, mpn=mpn, **overrides) for name in vendor_names]}}


class TestEnrichWithVendorCards:
    def test_empty_results_returns_early(self, db_session):
        results = {"LM317T": {"sightings": []}}
        _enrich_with_vendor_cards(results, db_session)
        # Should not raise — just returns early

    def test_no_vendor_names_returns_early(self, db_session):
        results = {"LM317T": {"sightings": [{"vendor_name": None, "mpn_matched": "LM317T"}]}}
        _enrich_with_vendor_cards(results, db_session)
        # Should return early with no DB queries

    def test_known_vendor_gets_card_summary(self, db_session):
        _make_card(db_session, "arrow electronics", "Arrow Electronics", vendor_score=75.0)
        results = _make_results("Arrow Electronics")
        _enrich_with_vendor_cards(results, db_session)
        sighting = results["LM317T"]["sightings"][0]
        assert "vendor_card" in sighting
        assert sighting["vendor_card"]["vendor_score"] == 75.0

    def test_unknown_vendor_gets_auto_created_card(self, db_session):
        results = _make_results("Brand New Supplier Inc.")
        _enrich_with_vendor_cards(results, db_session)
        # Card should have been created
        card = db_session.query(VendorCard).filter_by(normalized_name="brand new supplier").first()
        assert card is not None
        sighting = results["LM317T"]["sightings"][0]
        assert "vendor_card" in sighting

    def test_blacklisted_vendor_filtered_out(self, db_session):
        _make_card(
            db_session,
            "blacklisted vendor",
            "Blacklisted Vendor",
            is_blacklisted=True,
        )
        results = _make_results("Blacklisted Vendor")
        _enrich_with_vendor_cards(results, db_session)
        assert results["LM317T"]["sightings"] == []
        assert results["LM317T"]["blacklisted_count"] == 1

    def test_garbage_vendor_name_filtered_out(self, db_session):
        results = _make_results("no seller listed")
        _enrich_with_vendor_cards(results, db_session)
        assert results["LM317T"]["sightings"] == []

    def test_empty_vendor_name_returns_early(self, db_session):
        # When ALL vendor names are empty/None, the function returns early
        # and the sightings are left unchanged (no enrichment, no filtering)
        results = _make_results("")
        _enrich_with_vendor_cards(results, db_session)
        # Returns early — sightings are untouched (no vendor_card key added)
        assert "vendor_card" not in results["LM317T"]["sightings"][0]

    def test_email_harvested_and_merged_into_card(self, db_session):
        _make_card(db_session, "harvest vendor", "Harvest Vendor", emails=[])
        results = _make_results("Harvest Vendor", vendor_email="sales@harvestvendor.com")
        _enrich_with_vendor_cards(results, db_session)
        card = db_session.query(VendorCard).filter_by(normalized_name="harvest vendor").first()
        db_session.refresh(card)
        assert "sales@harvestvendor.com" in (card.emails or [])

    def test_phone_harvested_and_merged_into_card(self, db_session):
        _make_card(db_session, "phone vendor", "Phone Vendor", phones=[])
        results = _make_results("Phone Vendor", vendor_phone="+1-555-0100")
        _enrich_with_vendor_cards(results, db_session)
        card = db_session.query(VendorCard).filter_by(normalized_name="phone vendor").first()
        db_session.refresh(card)
        assert "+1-555-0100" in (card.phones or [])

    def test_website_set_on_card_if_missing(self, db_session):
        _make_card(db_session, "web vendor", "Web Vendor", website=None)
        results = _make_results("Web Vendor", vendor_url="https://webvendor.com")
        _enrich_with_vendor_cards(results, db_session)
        card = db_session.query(VendorCard).filter_by(normalized_name="web vendor").first()
        db_session.refresh(card)
        assert card.website == "https://webvendor.com"

    def test_historical_sightings_not_counted_for_mpn(self, db_session):
        _make_card(db_session, "hist vendor", "Hist Vendor")
        results = _make_results("Hist Vendor", vendor_email="hist@vendor.com", is_historical=True)
        _enrich_with_vendor_cards(results, db_session)
        # Historical sightings should still get enriched but not counted for mpn_count

    def test_review_count_and_avg_rating_in_summary(self, db_session, test_user):
        from app.models.vendors import VendorReview

        card = _make_card(db_session, "reviewed vendor", "Reviewed Vendor")

        review = VendorReview(
            vendor_card_id=card.id,
            user_id=test_user.id,
            rating=4,
        )
        db_session.add(review)
        db_session.commit()

        results = _make_results("Reviewed Vendor")
        _enrich_with_vendor_cards(results, db_session)
        sighting = results["LM317T"]["sightings"][0]
        vc = sighting["vendor_card"]
        assert vc["review_count"] == 1
        assert vc["avg_rating"] == 4.0

    def test_vendor_with_existing_emails_shown_in_summary(self, db_session):
        _make_card(
            db_session,
            "email vendor",
            "Email Vendor",
            emails=["contact@emailvendor.com"],
        )
        results = _make_results("Email Vendor")
        _enrich_with_vendor_cards(results, db_session)
        sighting = results["LM317T"]["sightings"][0]
        assert sighting["vendor_card"]["has_emails"] is True
        assert sighting["vendor_card"]["email_count"] == 1

    def test_multiple_groups_each_enriched(self, db_session):
        _make_card(db_session, "arrow electronics", "Arrow Electronics")
        results = {
            **_make_results("Arrow Electronics", mpn="LM317T"),
            **_make_results("Arrow Electronics", mpn="BC547"),
        }
        _enrich_with_vendor_cards(results, db_session)
        assert "vendor_card" in results["LM317T"]["sightings"][0]
        assert "vendor_card" in results["BC547"]["sightings"][0]

    def test_commit_failure_rolls_back(self, db_session):
        """Lines 319-321: when db.commit() raises, rollback is called."""
        _make_card(db_session, "rollback vendor", "Rollback Vendor")
        results = _make_results("Rollback Vendor")
        # New email triggers dirty flag → commit path
        results["LM317T"]["sightings"][0]["vendor_email"] = "new@rollback.com"
        with patch.object(db_session, "commit", side_effect=Exception("DB error")):
            with patch.object(db_session, "rollback") as mock_rollback:
                _enrich_with_vendor_cards(results, db_session)
        mock_rollback.assert_called_once()

    def test_sighting_count_incremented(self, db_session):
        card = _make_card(db_session, "count vendor", "Count Vendor", sighting_count=5)
        results = _make_results("Count Vendor")
        _enrich_with_vendor_cards(results, db_session)
        db_session.refresh(card)
        assert card.sighting_count > 5
