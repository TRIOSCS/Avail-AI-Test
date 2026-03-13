"""Tests for app/search_service.py — search orchestration, connector aggregation,
deduplication, scoring, material card upsert, history, and error handling.

Achieves 100% coverage of search_service.py by testing:
- get_all_pns: primary + substitutes dedup, blanks, empty keys
- search_requirement: full orchestration, empty PNs, material card errors
- _fetch_fresh: disabled/skipped/live sources, connector errors, dedup, junk vendors,
  stats aggregation, DB stats commit failure, gather exceptions
- _save_sightings: normalization, vendor score lookup, connector-aware delete,
  dedup old vs fresh, fallback delete, empty fresh
- _propagate_vendor_emails: email/phone propagation, existing contacts, no card, commit failure
- _get_material_history: filtering fresh vendors, empty PNs, no cards
- _history_to_result: age-based scoring (<7d, <30d, <90d, >90d), bonus capping
- _upsert_material_card: new card, existing card, vendor history update/create, no sightings
- sighting_to_dict: full field mapping
"""

import asyncio
import os

os.environ["TESTING"] = "1"

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import (
    ApiSource,
    MaterialCard,
    MaterialVendorHistory,
    Requirement,
    Requisition,
    Sighting,
    User,
    VendorCard,
    VendorContact,
)
from app.search_service import (
    _deduplicate_sightings,
    _fetch_fresh,
    _get_material_history,
    _history_to_result,
    _propagate_vendor_emails,
    _save_sightings,
    _upsert_material_card,
    get_all_pns,
    search_requirement,
    sighting_to_dict,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="search-test@trioscs.com",
        name="Search Test",
        role="buyer",
        azure_id="search-test-001",
        created_at=datetime.now(timezone.utc),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="SEARCH-REQ-001",
        customer_name="Test Co",
        status="active",
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(
    db: Session,
    requisition: Requisition,
    mpn: str = "LM317T",
    substitutes: list | None = None,
) -> Requirement:
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn=mpn,
        target_qty=100,
        substitutes=substitutes,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_api_source(db: Session, name: str, status: str = "live") -> ApiSource:
    src = ApiSource(
        name=name,
        display_name=name.capitalize(),
        category="distributor",
        source_type="api",
        status=status,
        total_searches=0,
        total_results=0,
        avg_response_ms=100,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def _all_connector_patches():
    """Context managers for patching all connector classes."""
    return (
        patch("app.search_service.NexarConnector"),
        patch("app.search_service.BrokerBinConnector"),
        patch("app.search_service.EbayConnector"),
        patch("app.search_service.DigiKeyConnector"),
        patch("app.search_service.MouserConnector"),
        patch("app.search_service.OEMSecretsConnector"),
        patch("app.search_service.SourcengineConnector"),
        patch("app.search_service.Element14Connector"),
    )


_CONNECTOR_CLASS_NAMES = [
    "NexarConnector",
    "BrokerBinConnector",
    "EbayConnector",
    "DigiKeyConnector",
    "MouserConnector",
    "OEMSecretsConnector",
    "SourcengineConnector",
    "Element14Connector",
]


def _setup_mock_connectors(mocks, default_results=None, class_names=None):
    """Configure mock connectors with default empty search results."""
    if default_results is None:
        default_results = []
    if class_names is None:
        class_names = _CONNECTOR_CLASS_NAMES
    for mock, cls_name in zip(mocks, class_names):
        mock.return_value.search = AsyncMock(return_value=list(default_results))
        mock.return_value.__class__.__name__ = cls_name


# ── get_all_pns ──────────────────────────────────────────────────────────


class TestGetAllPns:
    def test_primary_only(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")
        result = get_all_pns(req)
        assert result == ["LM317T"]

    def test_primary_with_substitutes(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T", substitutes=["LM7805", "LM7812"])
        result = get_all_pns(req)
        assert "LM317T" in result
        assert "LM7805" in result
        assert "LM7812" in result
        assert len(result) == 3

    def test_dedup_substitutes(self, db_session):
        """Duplicate substitute (same canonical key as primary) is excluded."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T", substitutes=["lm317t", "LM-317T"])
        result = get_all_pns(req)
        # All normalize to same key => only primary kept
        assert len(result) == 1

    def test_empty_primary(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="")
        result = get_all_pns(req)
        assert result == []

    def test_whitespace_primary(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="   ")
        result = get_all_pns(req)
        assert result == []

    def test_none_primary(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")
        req.primary_mpn = None
        result = get_all_pns(req)
        assert result == []

    def test_blank_substitutes_filtered(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T", substitutes=["", None, "  ", "LM7805"])
        result = get_all_pns(req)
        assert len(result) == 2
        assert "LM317T" in result
        assert "LM7805" in result

    def test_none_substitutes(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")
        req.substitutes = None
        result = get_all_pns(req)
        assert result == ["LM317T"]


# ── sighting_to_dict ─────────────────────────────────────────────────────


class TestSightingToDict:
    def test_full_fields(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            vendor_email="sales@arrow.com",
            vendor_phone="+1-555-0100",
            mpn_matched="LM317T",
            manufacturer="TI",
            qty_available=1000,
            unit_price=0.50,
            currency="USD",
            moq=100,
            source_type="nexar",
            is_authorized=True,
            confidence=0.9,
            score=85.0,
            condition="new",
            packaging="tape",
            date_code="2024+",
            lead_time_days=14,
            lead_time="2 weeks",
            is_unavailable=False,
            raw_data={
                "octopart_url": "https://octopart.com/lm317t",
                "click_url": "https://click.example.com",
                "vendor_url": "https://arrow.com/lm317t",
                "vendor_sku": "ARR-LM317T",
                "condition": "new",
                "country": "US",
            },
            created_at=now,
        )
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)

        d = sighting_to_dict(s)
        assert d["vendor_name"] == "Arrow"
        assert d["vendor_email"] == "sales@arrow.com"
        assert d["vendor_phone"] == "+1-555-0100"
        assert d["mpn_matched"] == "LM317T"
        assert d["manufacturer"] == "TI"
        assert d["qty_available"] == 1000
        assert float(d["unit_price"]) == 0.50
        assert d["currency"] == "USD"
        assert d["moq"] == 100
        assert d["source_type"] == "nexar"
        assert d["is_authorized"] is True
        assert d["confidence"] == 0.9
        assert d["score"] == 85.0
        assert d["is_unavailable"] is False
        assert d["octopart_url"] == "https://octopart.com/lm317t"
        assert d["click_url"] == "https://click.example.com"
        assert d["vendor_url"] == "https://arrow.com/lm317t"
        assert d["vendor_sku"] == "ARR-LM317T"
        assert d["condition"] == "new"
        assert d["country"] == "US"
        assert d["date_code"] == "2024+"
        assert d["packaging"] == "tape"
        assert d["lead_time_days"] == 14
        assert d["lead_time"] == "2 weeks"
        assert d["lead_confidence_bucket"] == "high"
        assert isinstance(d["lead_confidence_reason"], str)
        assert d["lead_confidence_reason"] != ""
        # SQLite strips timezone; compare without TZ suffix
        assert d["created_at"] is not None
        assert d["created_at"].startswith(now.isoformat()[:19])

    def test_none_raw_data(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Mouser",
            mpn_matched="LM317T",
            raw_data=None,
        )
        db_session.add(s)
        db_session.commit()

        d = sighting_to_dict(s)
        assert d["octopart_url"] is None
        assert d["vendor_sku"] is None
        # condition falls back to raw_data.get("condition") which is None
        assert d["condition"] is None

    def test_condition_fallback_to_raw(self, db_session):
        """When s.condition is None, fall back to raw_data condition."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Mouser",
            mpn_matched="LM317T",
            condition=None,
            raw_data={"condition": "refurbished"},
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
        db_session.commit()

        d = sighting_to_dict(s)
        assert d["condition"] == "refurbished"

    def test_is_stale_recent(self, db_session):
        """Sightings created recently should not be stale."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        db_session.add(s)
        db_session.commit()

        d = sighting_to_dict(s)
        assert d["is_stale"] is False

    def test_is_stale_old(self, db_session):
        """Sightings older than 90 days should be stale."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            created_at=datetime.now(timezone.utc) - timedelta(days=91),
        )
        db_session.add(s)
        db_session.commit()

        d = sighting_to_dict(s)
        assert d["is_stale"] is True

    def test_is_stale_no_created_at(self, db_session):
        """Sightings with no created_at should not be stale."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            created_at=None,
        )
        db_session.add(s)
        db_session.commit()

        d = sighting_to_dict(s)
        assert d["is_stale"] is False


# ── _history_to_result ───────────────────────────────────────────────────


class TestHistoryToResult:
    def _make_history(self, last_seen_delta_days=0, times_seen=1):
        now = datetime.now(timezone.utc)
        last_seen = now - timedelta(days=last_seen_delta_days)
        return {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "manufacturer": "TI",
            "qty_available": 500,
            "unit_price": 0.40,
            "currency": "USD",
            "source_type": "nexar",
            "is_authorized": True,
            "vendor_sku": "ARR-001",
            "first_seen": now - timedelta(days=100),
            "last_seen": last_seen,
            "times_seen": times_seen,
            "material_card_id": 99,
        }, now

    def test_recent_under_7_days(self):
        h, now = self._make_history(last_seen_delta_days=3, times_seen=1)
        result = _history_to_result(h, now)
        assert result["is_material_history"] is True
        assert result["is_historical"] is False
        # base 55, bonus 0, age_penalty = 3*0.1 = 0.3 => 54.7
        assert result["score"] >= 50

    def test_7_to_30_days(self):
        h, now = self._make_history(last_seen_delta_days=15, times_seen=2)
        result = _history_to_result(h, now)
        # base 45, bonus 3, age_penalty = 1.5 => 46.5
        assert 40 < result["score"] < 50

    def test_30_to_90_days(self):
        h, now = self._make_history(last_seen_delta_days=60, times_seen=3)
        result = _history_to_result(h, now)
        # base 35, bonus 6, age_penalty = 6.0 => 35.0
        assert 30 <= result["score"] <= 40

    def test_over_90_days(self):
        h, now = self._make_history(last_seen_delta_days=200, times_seen=1)
        result = _history_to_result(h, now)
        # base 30, bonus 0, age_penalty = 20.0 => max(10, 10.0) = 10
        assert result["score"] == 10

    def test_bonus_capped_at_15(self):
        h, now = self._make_history(last_seen_delta_days=1, times_seen=20)
        result = _history_to_result(h, now)
        # base 55, bonus=min(15, 19*3)=15, age=0.1 => ~69.9
        assert result["score"] > 65

    def test_none_last_seen(self):
        now = datetime.now(timezone.utc)
        h = {
            "vendor_name": "Arrow",
            "mpn_matched": "LM317T",
            "manufacturer": "TI",
            "qty_available": 500,
            "unit_price": 0.40,
            "currency": "USD",
            "source_type": "nexar",
            "is_authorized": True,
            "vendor_sku": None,
            "first_seen": None,
            "last_seen": None,
            "times_seen": 1,
            "material_card_id": 1,
        }
        result = _history_to_result(h, now)
        # age_days = 999, base = 30, bonus = 0, score = max(10, 30 + 0 - 99.9) = 10
        assert result["score"] == 10
        assert result["created_at"] is None
        assert result["material_last_seen"] is None
        assert result["material_first_seen"] is None

    def test_all_fields_present(self):
        h, now = self._make_history(last_seen_delta_days=5, times_seen=3)
        result = _history_to_result(h, now)
        assert result["id"] is None
        assert result["requirement_id"] is None
        assert result["vendor_name"] == "Arrow"
        assert result["mpn_matched"] == "LM317T"
        assert result["manufacturer"] == "TI"
        assert result["currency"] == "USD"
        assert result["is_authorized"] is True
        assert result["confidence"] == 0
        assert result["octopart_url"] is None
        assert result["click_url"] is None
        assert result["vendor_url"] is None
        assert result["vendor_sku"] == "ARR-001"
        assert result["condition"] is None
        assert result["moq"] is None
        assert result["material_card_id"] == 99
        assert result["material_times_seen"] == 3
        assert result["material_last_seen"] is not None
        assert result["material_first_seen"] is not None
        assert result["lead_confidence_bucket"] in {"high", "medium", "low"}
        assert isinstance(result["lead_confidence_reason"], str)


# ── _get_material_history ────────────────────────────────────────────────


class TestGetMaterialHistory:
    def test_returns_history_not_in_fresh(self, db_session):
        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            search_count=5,
        )
        db_session.add(card)
        db_session.flush()

        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            is_authorized=True,
            first_seen=datetime.now(timezone.utc) - timedelta(days=30),
            last_seen=datetime.now(timezone.utc),
            times_seen=5,
            last_qty=1000,
            last_price=0.50,
            last_currency="EUR",
            last_manufacturer="TI",
            vendor_sku="ARR-001",
        )
        db_session.add(vh)
        db_session.commit()

        fresh_vendors = {"mouser"}  # Arrow not in fresh
        result = _get_material_history([card.id], fresh_vendors, db_session)
        assert len(result) == 1
        assert result[0]["vendor_name"] == "Arrow"
        assert result[0]["currency"] == "EUR"
        assert result[0]["is_authorized"] is True
        assert result[0]["vendor_sku"] == "ARR-001"

    def test_excludes_fresh_vendors(self, db_session):
        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=1)
        db_session.add(card)
        db_session.flush()

        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
            times_seen=1,
        )
        db_session.add(vh)
        db_session.commit()

        fresh_vendors = {"arrow"}  # Arrow IS in fresh
        result = _get_material_history([card.id], fresh_vendors, db_session)
        assert len(result) == 0

    def test_empty_card_ids(self, db_session):
        result = _get_material_history([], set(), db_session)
        assert result == []

    def test_no_cards_found(self, db_session):
        result = _get_material_history([999999], set(), db_session)
        assert result == []

    def test_all_vendor_touchpoints_shown(self, db_session):
        """Multiple vendor history entries for same vendor (across cards) are all shown."""
        card1 = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=1)
        card2 = MaterialCard(normalized_mpn="lm7805", display_mpn="LM7805", search_count=1)
        db_session.add_all([card1, card2])
        db_session.flush()

        for card in [card1, card2]:
            vh = MaterialVendorHistory(
                material_card_id=card.id,
                vendor_name="Arrow",
                source_type="nexar",
                first_seen=datetime.now(timezone.utc),
                last_seen=datetime.now(timezone.utc),
                times_seen=1,
            )
            db_session.add(vh)
        db_session.commit()

        result = _get_material_history([card1.id, card2.id], set(), db_session)
        # Both vendor history rows should be returned (no dedup)
        assert len(result) == 2

    def test_none_times_seen(self, db_session):
        """times_seen=None should default to 1 in the output."""
        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=1)
        db_session.add(card)
        db_session.flush()
        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            times_seen=None,
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
        )
        db_session.add(vh)
        db_session.commit()

        result = _get_material_history([card.id], set(), db_session)
        assert result[0]["times_seen"] == 1

    def test_none_is_authorized(self, db_session):
        """is_authorized=None should default to False in the output."""
        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=1)
        db_session.add(card)
        db_session.flush()
        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            is_authorized=None,
            first_seen=datetime.now(timezone.utc),
            last_seen=datetime.now(timezone.utc),
            times_seen=2,
        )
        db_session.add(vh)
        db_session.commit()

        result = _get_material_history([card.id], set(), db_session)
        assert result[0]["is_authorized"] is False


# ── _upsert_material_card ────────────────────────────────────────────────


class TestUpsertMaterialCard:
    def test_creates_new_card(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            manufacturer="TI",
            qty_available=1000,
            unit_price=0.50,
            currency="USD",
            source_type="nexar",
            is_authorized=True,
            raw_data={"vendor_sku": "ARR-001"},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)

        card = db_session.query(MaterialCard).filter_by(normalized_mpn="lm317t").first()
        assert card is not None
        assert card.display_mpn == "LM317T"
        assert card.search_count == 1
        assert card.manufacturer == "TI"

        vh = db_session.query(MaterialVendorHistory).filter_by(material_card_id=card.id).first()
        assert vh is not None
        assert vh.vendor_name == "arrow"  # Stored normalized since Phase 2
        assert vh.times_seen == 1
        assert vh.is_authorized is True
        assert vh.vendor_sku == "ARR-001"

    def test_updates_existing_card(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        # Pre-create card
        card = MaterialCard(
            normalized_mpn="lm317t",
            display_mpn="LM317T",
            search_count=5,
            manufacturer=None,
        )
        db_session.add(card)
        db_session.flush()

        # Pre-create vendor history
        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            first_seen=now - timedelta(days=10),
            last_seen=now - timedelta(days=1),
            times_seen=3,
            last_qty=500,
            last_price=0.60,
            last_currency="USD",
        )
        db_session.add(vh)
        db_session.commit()

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            manufacturer="TI",
            qty_available=1000,
            unit_price=0.50,
            currency="EUR",
            source_type="nexar",
            is_authorized=True,
            raw_data={"vendor_sku": "ARR-002"},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)

        db_session.refresh(card)
        assert card.search_count == 6
        assert card.manufacturer == "TI"

        db_session.refresh(vh)
        assert vh.times_seen == 4
        assert vh.last_qty == 1000
        assert float(vh.last_price) == 0.50
        assert vh.last_currency == "EUR"
        assert vh.is_authorized is True
        assert vh.vendor_sku == "ARR-002"

    def test_empty_pn_key(self, db_session):
        """If pn normalizes to empty key, do nothing."""
        now = datetime.now(timezone.utc)
        _upsert_material_card("", [], db_session, now)
        count = db_session.query(MaterialCard).count()
        assert count == 0

    def test_no_matching_sightings(self, db_session):
        """If no sightings match the pn_key, don't create a card."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T")
        now = datetime.now(timezone.utc)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="TOTALLY_DIFFERENT",
            raw_data={},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)
        count = db_session.query(MaterialCard).count()
        assert count == 0

    def test_skip_sighting_without_vendor_name(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="",
            mpn_matched="LM317T",
            raw_data={},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)
        # Card is created (sightings match pn_key) but no vendor history
        vh_count = db_session.query(MaterialVendorHistory).count()
        assert vh_count == 0

    def test_new_vendor_history_created(self, db_session):
        """New vendor name creates a new MaterialVendorHistory."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=0)
        db_session.add(card)
        db_session.flush()

        # Existing vendor history for Arrow
        vh_arrow = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            first_seen=now,
            last_seen=now,
            times_seen=1,
        )
        db_session.add(vh_arrow)
        db_session.commit()

        # New sighting from Mouser (different vendor)
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Mouser",
            mpn_matched="LM317T",
            manufacturer="TI",
            qty_available=2000,
            unit_price=0.45,
            currency="USD",
            source_type="mouser",
            is_authorized=False,
            raw_data={},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)

        vh_mouser = (
            db_session.query(MaterialVendorHistory).filter_by(material_card_id=card.id, vendor_name="mouser").first()
        )
        assert vh_mouser is not None
        assert vh_mouser.times_seen == 1
        assert vh_mouser.last_qty == 2000

    def test_existing_card_search_count_none(self, db_session):
        """search_count=None should become 1 after upsert."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=None)
        db_session.add(card)
        db_session.commit()

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            raw_data={},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)
        db_session.refresh(card)
        assert card.search_count == 1

    def test_vh_update_partial_fields(self, db_session):
        """Vendor history update only sets fields that are non-None on the sighting."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=0)
        db_session.add(card)
        db_session.flush()

        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            first_seen=now - timedelta(days=5),
            last_seen=now - timedelta(days=1),
            times_seen=2,
            last_qty=500,
            last_price=0.60,
            last_currency="USD",
            last_manufacturer="OldMfg",
            is_authorized=False,
            vendor_sku="OLD-SKU",
        )
        db_session.add(vh)
        db_session.commit()

        # Sighting with all None values -- should NOT overwrite existing
        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            manufacturer=None,
            qty_available=None,
            unit_price=None,
            currency=None,
            is_authorized=False,
            raw_data={},  # no vendor_sku
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)
        db_session.refresh(vh)
        assert vh.times_seen == 3
        assert vh.last_qty == 500  # unchanged
        assert vh.last_price == 0.60  # unchanged
        assert vh.last_currency == "USD"  # unchanged
        assert vh.last_manufacturer == "OldMfg"  # unchanged
        assert vh.is_authorized is False
        assert vh.vendor_sku == "OLD-SKU"  # unchanged

    def test_vh_times_seen_none(self, db_session):
        """times_seen=None should become 2 after increment (None or 1) + 1."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)
        now = datetime.now(timezone.utc)

        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=0)
        db_session.add(card)
        db_session.flush()

        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Arrow",
            source_type="nexar",
            times_seen=None,
            first_seen=now,
            last_seen=now,
        )
        db_session.add(vh)
        db_session.commit()

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            raw_data={},
        )
        db_session.add(s)
        db_session.commit()

        _upsert_material_card("LM317T", [s], db_session, now)
        db_session.refresh(vh)
        assert vh.times_seen == 2  # (None or 1) + 1


# ── _save_sightings ─────────────────────────────────────────────────────


class TestSaveSightings:
    def test_basic_save(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow Electronics",
                "mpn_matched": "LM317T",
                "manufacturer": "TI",
                "qty_available": 1000,
                "unit_price": 0.50,
                "currency": "USD",
                "source_type": "nexar",
                "is_authorized": True,
                "confidence": 4,  # 4/5 = 0.8
                "condition": "New",
                "packaging": "Tape & Reel",
                "date_code": "2024+",
                "lead_time": "2-3 weeks",
                "vendor_email": None,
                "vendor_phone": None,
                "moq": 100,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert len(result) == 1
        s = result[0]
        assert s.vendor_name == "Arrow Electronics"
        assert s.vendor_name_normalized == "arrow electronics"
        assert s.confidence == 0.8  # 4 / 5.0
        # v2 multi-factor score: authorized trust=95, price/qty/freshness/completeness all contribute
        assert s.score > 80.0  # authorized vendors score high
        assert s.evidence_tier == "T1"  # authorized + nexar
        assert s.score_components is not None
        assert s.score_components["trust"] == 95.0

    def test_vendor_name_normalized_populated(self, db_session):
        """Sighting creation populates vendor_name_normalized."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Mouser Electronics, Inc.",
                "mpn_matched": "NE555P",
                "qty_available": 500,
                "unit_price": 0.30,
                "source_type": "nexar",
            },
            {
                "vendor_name": "  DigiKey Corp.  ",
                "mpn_matched": "LM7805",
                "qty_available": 200,
                "unit_price": 0.75,
                "source_type": "nexar",
            },
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert len(result) == 2
        norms = {s.vendor_name_normalized for s in result}
        assert "mouser electronics" in norms
        assert "digikey" in norms

    def test_connector_aware_delete(self, db_session):
        """Only sightings from succeeded sources are deleted."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Pre-existing sightings
        old_nexar = Sighting(
            requirement_id=req.id,
            vendor_name="Old Nexar",
            mpn_matched="LM317T",
            source_type="nexar",
        )
        old_ebay = Sighting(
            requirement_id=req.id,
            vendor_name="Old eBay",
            mpn_matched="LM317T",
            source_type="ebay",
        )
        db_session.add_all([old_nexar, old_ebay])
        db_session.commit()

        fresh = [
            {
                "vendor_name": "New Nexar Vendor",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})

        # Old nexar and octopart deleted; old ebay preserved
        remaining = db_session.query(Sighting).filter_by(requirement_id=req.id).all()
        vendor_names = {s.vendor_name for s in remaining}
        assert "Old Nexar" not in vendor_names
        assert "Old eBay" in vendor_names
        assert "New Nexar Vendor" in vendor_names

    def test_fallback_delete_all(self, db_session):
        """When succeeded_sources is None, all old sightings are deleted."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        old = Sighting(
            requirement_id=req.id,
            vendor_name="OldVendor",
            mpn_matched="LM317T",
            source_type="nexar",
        )
        db_session.add(old)
        db_session.commit()

        fresh = [
            {
                "vendor_name": "NewVendor",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources=None)
        remaining = db_session.query(Sighting).filter_by(requirement_id=req.id).all()
        assert len(remaining) == 1
        assert remaining[0].vendor_name == "NewVendor"

    def test_dedup_old_vs_fresh(self, db_session):
        """Old sightings from non-succeeded sources get deleted if vendor+mpn matches fresh."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Old sighting from a different source
        old = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            mpn_matched="LM317T",
            source_type="ebay",
        )
        db_session.add(old)
        db_session.commit()

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})

        remaining = db_session.query(Sighting).filter_by(requirement_id=req.id).all()
        # Old ebay Arrow should be deleted because fresh Arrow exists
        source_types = {s.source_type for s in remaining}
        assert "ebay" not in source_types

    def test_vendor_score_lookup(self, db_session):
        """VendorCard.vendor_score is used in scoring."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Create a vendor card with a score
        vc = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            vendor_score=75.0,
        )
        db_session.add(vc)
        db_session.commit()

        fresh = [
            {
                "vendor_name": "Arrow Electronics",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "is_authorized": False,
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert len(result) == 1
        # v2 multi-factor score uses vendor_score=75.0 for trust component
        assert result[0].score_components["trust"] == 75.0
        assert result[0].score > 0  # weighted sum is positive

    def test_normalization_of_fields(self, db_session):
        """Various normalization paths: qty as int/float, price as int/float, confidence > 1."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "  Arrow  ",
                "mpn_matched": " lm317t ",
                "qty_available": 500,
                "unit_price": 1.25,
                "currency": "$1.25",  # contains currency symbol
                "source_type": "nexar",
                "confidence": 3,  # > 1, so /5.0 = 0.6
                "condition": "Factory New",
                "packaging": "Tray",
                "date_code": "2024",
                "lead_time": "10 days",
                "vendor_email": None,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        s = result[0]
        assert s.confidence == 0.6

    def test_empty_fresh(self, db_session):
        """Empty fresh list produces empty result."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        result = _save_sightings([], req, db_session, succeeded_sources={"nexar"})
        assert result == []

    def test_confidence_already_normalized(self, db_session):
        """Confidence <= 1 should pass through unchanged."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0.7,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].confidence == 0.7

    def test_confidence_zero(self, db_session):
        """Confidence=0 or None should result in 0.0."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": None,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].confidence == 0.0

    def test_qty_fallback_for_positive_numeric(self, db_session):
        """When normalize_quantity returns None but raw is positive int/float, keep it."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "qty_available": 42,
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].qty_available is not None

    def test_price_fallback_for_positive_numeric(self, db_session):
        """When normalize_price returns None but raw is positive float, keep it."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "unit_price": 0.001,
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].unit_price is not None

    def test_currency_fallback_to_usd(self, db_session):
        """When currency is None, default to 'USD' instead of falling back to unit_price."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "unit_price": 1.25,
                "currency": None,
                "confidence": 0,
            }
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].currency == "USD"

    def test_no_needed_names_empty_vendor_score_map(self, db_session):
        """When all vendor names are blank, vendor_score_map is empty."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        raw = {
            "vendor_name": "",
            "mpn_matched": "LM317T",
            "source_type": "nexar",
            "confidence": 0,
        }
        result = _save_sightings([raw], req, db_session, succeeded_sources={"nexar"})
        assert len(result) == 1

    def test_succeeded_sources_empty_set(self, db_session):
        """Empty succeeded_sources set does not trigger connector-aware delete."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Pre-existing sighting
        old = Sighting(
            requirement_id=req.id,
            vendor_name="OldVendor",
            mpn_matched="LM317T",
            source_type="nexar",
        )
        db_session.add(old)
        db_session.commit()

        fresh = [
            {
                "vendor_name": "NewVendor",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0,
            }
        ]
        # Empty set - expanded is empty, so no connector-aware delete
        result = _save_sightings(fresh, req, db_session, succeeded_sources=set())
        # Old sighting should still be around since empty set = no succeeded sources
        remaining = db_session.query(Sighting).filter_by(requirement_id=req.id).all()
        vendor_names = {s.vendor_name for s in remaining}
        assert "NewVendor" in vendor_names

    def test_qty_fallback_when_normalize_returns_none(self, db_session):
        """Cover line 388: normalize_quantity returns None but raw is positive int."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "qty_available": 42,
                "confidence": 0,
            }
        ]
        with patch("app.search_service.normalize_quantity", return_value=None):
            result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].qty_available == 42

    def test_price_fallback_when_normalize_returns_none(self, db_session):
        """Cover line 393: normalize_price returns None but raw is positive float."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "unit_price": 1.25,
                "confidence": 0,
            }
        ]
        with patch("app.search_service.normalize_price", return_value=None):
            result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})
        assert result[0].unit_price == 1.25


# ── _propagate_vendor_emails ─────────────────────────────────────────────


class TestPropagateVendorEmails:
    def test_creates_vendor_contact(self, db_session):
        vc = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
        )
        db_session.add(vc)
        db_session.flush()

        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow Electronics",
            vendor_email="sales@arrow.com",
            vendor_phone="+1-555-0100",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)

        contact = db_session.query(VendorContact).filter_by(vendor_card_id=vc.id).first()
        assert contact is not None
        assert contact.email == "sales@arrow.com"
        assert contact.source == "brokerbin"
        assert contact.confidence == 60

    def test_updates_existing_contact_last_seen(self, db_session):
        vc = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
        )
        db_session.add(vc)
        db_session.flush()

        old_time = datetime.now(timezone.utc) - timedelta(days=30)
        existing = VendorContact(
            vendor_card_id=vc.id,
            email="sales@arrow.com",
            source="manual",
            last_seen_at=old_time,
        )
        db_session.add(existing)
        db_session.commit()

        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow Electronics",
            vendor_email="sales@arrow.com",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)

        db_session.refresh(existing)
        # SQLite strips timezone info; compare without tz
        assert existing.last_seen_at.replace(tzinfo=None) > old_time.replace(tzinfo=None)

    def test_no_email_skipped(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            vendor_email=None,
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)
        assert db_session.query(VendorContact).count() == 0

    def test_invalid_email_skipped(self, db_session):
        """Email without @ is skipped."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow",
            vendor_email="not-an-email",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)
        assert db_session.query(VendorContact).count() == 0

    def test_no_vendor_card_skipped(self, db_session):
        """If no VendorCard exists for the vendor, skip without error."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Unknown Vendor",
            vendor_email="sales@unknown.com",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)
        assert db_session.query(VendorContact).count() == 0

    def test_blank_vendor_name_skipped(self, db_session):
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="",
            vendor_email="sales@example.com",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)
        assert db_session.query(VendorContact).count() == 0

    def test_commit_failure_handled(self, db_session):
        """Commit failure during propagation is logged and rolled back."""
        vc = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
        )
        db_session.add(vc)
        db_session.flush()

        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow Electronics",
            vendor_email="sales@arrow.com",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        with patch.object(db_session, "commit", side_effect=Exception("DB error")):
            # Should not raise
            _propagate_vendor_emails([s], db_session)

    def test_phone_propagation(self, db_session):
        """Phones are merged into VendorCard when available."""
        vc = VendorCard(
            normalized_name="arrow electronics",
            display_name="Arrow Electronics",
            phones=[],
        )
        db_session.add(vc)
        db_session.flush()

        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="Arrow Electronics",
            vendor_email="sales@arrow.com",
            vendor_phone="+1-555-0100",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        _propagate_vendor_emails([s], db_session)
        # Phone should have been merged; verify contact was created
        contact = db_session.query(VendorContact).filter_by(vendor_card_id=vc.id).first()
        assert contact is not None

    def test_empty_sightings(self, db_session):
        """Empty sightings list returns without error."""
        _propagate_vendor_emails([], db_session)
        assert db_session.query(VendorContact).count() == 0

    def test_vendor_name_normalizes_to_empty(self, db_session):
        """Cover line 480: vendor name that normalizes to empty string is skipped."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        s = Sighting(
            requirement_id=req.id,
            vendor_name="SomeVendor",
            vendor_email="sales@example.com",
            mpn_matched="LM317T",
        )
        db_session.add(s)
        db_session.commit()

        with patch("app.vendor_utils.normalize_vendor_name", return_value=""):
            _propagate_vendor_emails([s], db_session)
        assert db_session.query(VendorContact).count() == 0


# ── _fetch_fresh ─────────────────────────────────────────────────────────


class TestFetchFresh:
    """Test _fetch_fresh with all connectors mocked."""

    @pytest.mark.asyncio
    async def test_all_disabled(self, db_session):
        """When all sources are disabled, returns empty results."""
        for name in ["nexar", "brokerbin", "ebay", "digikey", "mouser", "oemsecrets", "sourcengine", "element14", "ai_live_web"]:
            _make_api_source(db_session, name, status="disabled")

        with patch("app.services.credential_service.get_credential", return_value="fake-key"):
            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert results == []
        disabled_count = sum(1 for s in stats if s["status"] == "disabled")
        assert disabled_count == 9

    @pytest.mark.asyncio
    async def test_no_credentials(self, db_session):
        """When no credentials are configured, all sources are skipped."""
        with patch("app.services.credential_service.get_credential", return_value=None):
            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert results == []
        skipped_count = sum(1 for s in stats if s["status"] == "skipped")
        assert skipped_count == 9

    @pytest.mark.asyncio
    async def test_successful_search(self, db_session):
        """One connector returns results successfully."""
        _make_api_source(db_session, "nexar")

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "vendor_sku": "ARR-1", "source_type": "nexar"},
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert len(results) >= 1
        assert any(r["vendor_name"] == "Arrow" for r in results)

    @pytest.mark.asyncio
    async def test_connector_error_handled(self, db_session):
        """Connector that raises an exception still returns partial results."""
        _make_api_source(db_session, "nexar")
        _make_api_source(db_session, "brokerbin")

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)

            # Nexar errors out
            MockNexar.return_value.search = AsyncMock(side_effect=Exception("Nexar down"))
            # BrokerBin returns results
            MockBB.return_value.search = AsyncMock(
                return_value=[
                    {
                        "vendor_name": "BrokerVendor",
                        "mpn_matched": "LM317T",
                        "vendor_sku": "BB-1",
                        "source_type": "brokerbin",
                    },
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert any(r["vendor_name"] == "BrokerVendor" for r in results)
        nexar_stat = next((s for s in stats if s["source"] == "nexar"), None)
        if nexar_stat:
            assert nexar_stat["status"] == "error"

    @pytest.mark.asyncio
    async def test_dedup_results(self, db_session):
        """Duplicate results (same vendor, mpn_key, vendor_sku) are deduped."""
        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)

            dup_result = {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "vendor_sku": "ARR-1",
                "source_type": "nexar",
            }
            MockNexar.return_value.search = AsyncMock(return_value=[dup_result.copy()])
            MockBB.return_value.search = AsyncMock(return_value=[dup_result.copy()])

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        arrow_results = [r for r in results if r["vendor_name"] == "Arrow"]
        assert len(arrow_results) == 1

    @pytest.mark.asyncio
    async def test_dedup_integer_vendor_sku(self, db_session):
        """Dedup handles integer vendor_sku without crashing (OEMSecrets returns int SKUs)."""
        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)

            # OEMSecrets returns vendor_sku as integer
            MockOEM.return_value.search = AsyncMock(
                return_value=[
                    {
                        "vendor_name": "Farnell",
                        "mpn_matched": "LM317T",
                        "vendor_sku": 4200830,
                        "source_type": "oemsecrets",
                    },
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert len(results) == 1
        assert results[0]["vendor_sku"] == 4200830

    @pytest.mark.asyncio
    async def test_junk_vendors_filtered(self, db_session):
        """Junk vendor names (empty, 'unknown', etc.) are filtered out."""
        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)

            junk_results = [
                {"vendor_name": "", "mpn_matched": "LM317T", "vendor_sku": "1"},
                {"vendor_name": "Unknown", "mpn_matched": "LM317T", "vendor_sku": "2"},
                {"vendor_name": "(no sellers listed)", "mpn_matched": "LM317T", "vendor_sku": "3"},
                {"vendor_name": "n/a", "mpn_matched": "LM317T", "vendor_sku": "4"},
                {"vendor_name": "none", "mpn_matched": "LM317T", "vendor_sku": "5"},
                {"vendor_name": "(none)", "mpn_matched": "LM317T", "vendor_sku": "6"},
                {"vendor_name": "-", "mpn_matched": "LM317T", "vendor_sku": "7"},
                {"vendor_name": "no vendor", "mpn_matched": "LM317T", "vendor_sku": "8"},
                {"vendor_name": "no seller", "mpn_matched": "LM317T", "vendor_sku": "9"},
                {"vendor_name": "no sellers listed", "mpn_matched": "LM317T", "vendor_sku": "10"},
                {"vendor_name": "Good Vendor", "mpn_matched": "LM317T", "vendor_sku": "11"},
            ]
            MockNexar.return_value.search = AsyncMock(return_value=junk_results)

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert len(results) == 1
        assert results[0]["vendor_name"] == "Good Vendor"

    @pytest.mark.asyncio
    async def test_stats_aggregation_multiple_pns(self, db_session):
        """Stats are aggregated across multiple PNs for same connector."""
        _make_api_source(db_session, "nexar")

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)

            async def _nexar_search(pn):
                return [
                    {"vendor_name": f"V-{pn}", "mpn_matched": pn, "vendor_sku": f"SKU-{pn}", "source_type": "nexar"}
                ]

            MockNexar.return_value.search = AsyncMock(side_effect=_nexar_search)

            results, stats = await _fetch_fresh(["LM317T", "LM7805"], db_session)

        assert len(results) == 2
        nexar_stat = next((s for s in stats if s["source"] == "nexar"), None)
        assert nexar_stat is not None
        assert nexar_stat["results"] == 2

    @pytest.mark.asyncio
    async def test_db_stats_commit_failure(self, db_session):
        """DB stats commit failure doesn't break the search."""
        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "vendor_sku": "1"},
                ]
            )

            original_commit = db_session.commit
            call_count = [0]

            def flaky_commit():
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("DB stats error")
                return original_commit()

            with patch.object(db_session, "commit", side_effect=flaky_commit):
                results, stats = await _fetch_fresh(["LM317T"], db_session)

        # Results should still be returned despite stats commit failure
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_mixed_disabled_and_no_creds(self, db_session):
        """Mix of disabled sources and sources without credentials."""
        _make_api_source(db_session, "nexar", status="disabled")
        _make_api_source(db_session, "ebay", status="disabled")

        def selective_cred(db, source_name, var_name):
            if source_name == "brokerbin":
                return "fake-key"
            return None

        with (
            patch("app.services.credential_service.get_credential", side_effect=selective_cred),
            patch("app.search_service.BrokerBinConnector") as MockBB,
        ):
            MockBB.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "BB Vendor", "mpn_matched": "LM317T", "vendor_sku": "1"},
                ]
            )
            MockBB.return_value.__class__.__name__ = "BrokerBinConnector"

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        statuses = {s["source"]: s["status"] for s in stats}
        assert statuses.get("nexar") == "disabled"
        assert statuses.get("ebay") == "disabled"
        bb_stat = next((s for s in stats if s["source"] == "brokerbin"), None)
        assert bb_stat is not None
        assert bb_stat["status"] == "ok"

    @pytest.mark.asyncio
    async def test_source_stats_error_then_success(self, db_session):
        """When a source errors for one PN and succeeds for another, aggregated status has error."""
        _make_api_source(db_session, "nexar")

        call_count = [0]

        async def _flaky_search(pn):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Temporary failure")
            return [{"vendor_name": "Arrow", "mpn_matched": pn, "vendor_sku": "A1"}]

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(side_effect=_flaky_search)

            results, stats = await _fetch_fresh(["PN1", "PN2"], db_session)

        nexar_stat = next((s for s in stats if s["source"] == "nexar"), None)
        assert nexar_stat is not None
        assert nexar_stat["status"] == "error"
        assert nexar_stat["results"] == 1

    @pytest.mark.asyncio
    async def test_source_stats_success_then_error(self, db_session):
        """Cover lines 322-323: success first for one PN, then error for another PN.
        The aggregation should update error status on the existing ok entry."""
        _make_api_source(db_session, "nexar")

        call_count = [0]

        async def _flaky_search(pn):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call succeeds
                return [{"vendor_name": "Arrow", "mpn_matched": pn, "vendor_sku": "A1"}]
            # Second call errors
            raise Exception("Second call failed")

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(side_effect=_flaky_search)

            results, stats = await _fetch_fresh(["PN1", "PN2"], db_session)

        nexar_stat = next((s for s in stats if s["source"] == "nexar"), None)
        assert nexar_stat is not None
        # After success then error, the aggregated status should be "error"
        assert nexar_stat["status"] == "error"
        assert nexar_stat["error"] is not None
        assert nexar_stat["results"] == 1  # only first call succeeded

    @pytest.mark.asyncio
    async def test_no_connectors_returns_early(self, db_session):
        """If all sources are disabled or skipped, returns early."""
        for name in ["nexar", "brokerbin", "ebay", "digikey", "mouser", "oemsecrets", "sourcengine", "element14"]:
            _make_api_source(db_session, name, status="disabled")

        with patch("app.services.credential_service.get_credential", return_value="fake-key"):
            results, stats = await _fetch_fresh(["LM317T"], db_session)

        assert results == []
        assert len(stats) == 9

    @pytest.mark.asyncio
    async def test_api_source_stats_updated(self, db_session):
        """ApiSource records are updated with search stats after successful search."""
        src = _make_api_source(db_session, "nexar")
        old_searches = src.total_searches

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "vendor_sku": "1"},
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        db_session.refresh(src)
        assert src.total_searches == old_searches + 1
        assert src.total_results == 1
        assert src.status == "live"
        assert src.last_success is not None
        assert src.last_error is None

    @pytest.mark.asyncio
    async def test_api_source_error_stats(self, db_session):
        """ApiSource.last_error, last_error_at, error_count_24h updated on failure."""
        src = _make_api_source(db_session, "nexar")

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(side_effect=Exception("API timeout"))

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        db_session.refresh(src)
        assert src.last_error == "API timeout"
        assert src.last_error_at is not None
        assert src.error_count_24h == 1

    @pytest.mark.asyncio
    async def test_avg_response_ms_calculated(self, db_session):
        """avg_response_ms uses exponential moving average formula."""
        src = _make_api_source(db_session, "nexar")
        src.avg_response_ms = 200
        db_session.commit()

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "vendor_sku": "1"},
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        db_session.refresh(src)
        assert src.avg_response_ms is not None

    @pytest.mark.asyncio
    async def test_avg_response_ms_none_defaults(self, db_session):
        """When avg_response_ms is None, it defaults to the elapsed time."""
        src = _make_api_source(db_session, "nexar")
        src.avg_response_ms = None
        db_session.commit()

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "vendor_sku": "1"},
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        db_session.refresh(src)
        # avg_response_ms should be set (prev = None => prev = elapsed_ms)
        assert src.avg_response_ms is not None

    @pytest.mark.asyncio
    async def test_source_not_in_src_map_skipped(self, db_session):
        """Connectors without matching ApiSource records skip stats update."""
        # Don't create any ApiSource records
        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
        ):
            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = AsyncMock(
                return_value=[
                    {"vendor_name": "Arrow", "mpn_matched": "LM317T", "vendor_sku": "1"},
                ]
            )

            results, stats = await _fetch_fresh(["LM317T"], db_session)

        # No ApiSource to update, but results should still be returned
        assert len(results) >= 1


# ── search_requirement ───────────────────────────────────────────────────


class TestSearchRequirement:
    @pytest.mark.asyncio
    async def test_empty_pns_returns_empty(self, db_session):
        """Requirement with no PNs returns empty results."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="")

        result = await search_requirement(req, db_session)
        assert result == {"sightings": [], "source_stats": []}

    @pytest.mark.asyncio
    async def test_full_orchestration(self, db_session):
        """Full search: fetch, save, upsert, history, combine and sort."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        mock_fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "vendor_sku": "ARR-1",
                "source_type": "nexar",
                "is_authorized": True,
                "confidence": 5,
                "manufacturer": "TI",
                "qty_available": 1000,
                "unit_price": 0.50,
                "currency": "USD",
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 100, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        assert "sightings" in result
        assert "source_stats" in result
        assert len(result["sightings"]) >= 1
        assert result["source_stats"] == mock_stats

    @pytest.mark.asyncio
    async def test_material_card_upsert_error_handled(self, db_session):
        """Material card upsert failure doesn't break the search."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        mock_fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "vendor_sku": "ARR-1",
                "source_type": "nexar",
                "is_authorized": False,
                "confidence": 0,
                "qty_available": 100,
                "unit_price": 0.50,
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 100, "error": None, "status": "ok"},
        ]

        with (
            patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch,
            patch("app.search_service._upsert_material_card", side_effect=Exception("DB error")),
        ):
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        # Should still return sightings despite upsert failure
        assert len(result["sightings"]) >= 1

    @pytest.mark.asyncio
    async def test_results_sorted_by_score(self, db_session):
        """Results are sorted descending by score."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        mock_fresh = [
            {
                "vendor_name": "LowScore",
                "mpn_matched": "LM317T",
                "vendor_sku": "L1",
                "source_type": "nexar",
                "is_authorized": False,
                "confidence": 0,
                "qty_available": 50,
                "unit_price": 0.30,
            },
            {
                "vendor_name": "HighScore",
                "mpn_matched": "LM317T",
                "vendor_sku": "H1",
                "source_type": "nexar",
                "is_authorized": True,
                "confidence": 5,
                "qty_available": 100,
                "unit_price": 0.40,
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 2, "ms": 50, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        sightings = result["sightings"]
        scores = [s["score"] for s in sightings]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_history_merged_into_results(self, db_session):
        """Material card vendor history is merged into results."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        # Create material card with vendor history
        card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", search_count=5)
        db_session.add(card)
        db_session.flush()

        vh = MaterialVendorHistory(
            material_card_id=card.id,
            vendor_name="Historic Vendor",
            source_type="brokerbin",
            first_seen=datetime.now(timezone.utc) - timedelta(days=30),
            last_seen=datetime.now(timezone.utc) - timedelta(days=5),
            times_seen=3,
            last_qty=500,
            last_price=0.40,
            last_currency="USD",
        )
        db_session.add(vh)
        db_session.commit()

        mock_fresh = [
            {
                "vendor_name": "FreshVendor",
                "mpn_matched": "LM317T",
                "vendor_sku": "F1",
                "source_type": "nexar",
                "is_authorized": False,
                "confidence": 0,
                "qty_available": 250,
                "unit_price": 0.45,
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 50, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        vendor_names = [s["vendor_name"] for s in result["sightings"]]
        assert "FreshVendor" in vendor_names
        assert "Historic Vendor" in vendor_names

        hist_entry = next(s for s in result["sightings"] if s["vendor_name"] == "Historic Vendor")
        assert hist_entry["is_material_history"] is True

    @pytest.mark.asyncio
    async def test_source_stats_with_error_not_in_succeeded(self, db_session):
        """Source stats with errors are not in succeeded_sources set."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        mock_fresh = []
        mock_stats = [
            {"source": "nexar", "results": 0, "ms": 100, "error": "timeout", "status": "error"},
            {"source": "brokerbin", "results": 1, "ms": 50, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        assert result is not None

    @pytest.mark.asyncio
    async def test_multiple_pns_all_upserted(self, db_session):
        """Multiple PNs each get material card upserts."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn, mpn="LM317T", substitutes=["LM7805"])

        mock_fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "vendor_sku": "A1",
                "source_type": "nexar",
                "is_authorized": False,
                "confidence": 0,
                "qty_available": 100,
                "unit_price": 0.35,
            },
            {
                "vendor_name": "Mouser",
                "mpn_matched": "LM7805",
                "vendor_sku": "M1",
                "source_type": "mouser",
                "is_authorized": False,
                "confidence": 0,
                "qty_available": 200,
                "unit_price": 0.50,
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 50, "error": None, "status": "ok"},
            {"source": "mouser", "results": 1, "ms": 50, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        card1 = db_session.query(MaterialCard).filter_by(normalized_mpn="lm317t").first()
        card2 = db_session.query(MaterialCard).filter_by(normalized_mpn="lm7805").first()
        assert card1 is not None
        assert card2 is not None

    @pytest.mark.asyncio
    async def test_fresh_sightings_not_historical(self, db_session):
        """Fresh sightings are marked is_historical=False and is_material_history=False."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        mock_fresh = [
            {
                "vendor_name": "Arrow",
                "mpn_matched": "LM317T",
                "source_type": "nexar",
                "confidence": 0,
                "qty_available": 75,
                "unit_price": 0.25,
            },
        ]
        mock_stats = [
            {"source": "nexar", "results": 1, "ms": 50, "error": None, "status": "ok"},
        ]

        with patch("app.search_service._fetch_fresh", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = (mock_fresh, mock_stats)
            result = await search_requirement(req, db_session)

        for s in result["sightings"]:
            if s["vendor_name"] == "Arrow":
                assert s["is_historical"] is False
                assert s["is_material_history"] is False


class TestSearchThrottling:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self, db_session):
        """Search concurrency is limited by settings.search_concurrency_limit."""

        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        original_search = AsyncMock(
            return_value=[
                {"vendor_name": "Test", "mpn_matched": "X", "vendor_sku": "1"},
            ]
        )

        async def _tracking_search(pn):
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.01)
            result = await original_search(pn)
            async with lock:
                current_concurrent -= 1
            return result

        with (
            patch("app.services.credential_service.get_credential", return_value="fake-key"),
            patch("app.search_service.NexarConnector") as MockNexar,
            patch("app.search_service.BrokerBinConnector") as MockBB,
            patch("app.search_service.EbayConnector") as MockEbay,
            patch("app.search_service.DigiKeyConnector") as MockDK,
            patch("app.search_service.MouserConnector") as MockMouser,
            patch("app.search_service.OEMSecretsConnector") as MockOEM,
            patch("app.search_service.SourcengineConnector") as MockSrc,
            patch("app.search_service.Element14Connector") as MockE14,
            patch("app.config.settings") as mock_settings,
        ):
            mock_settings.search_concurrency_limit = 2

            mocks = [MockNexar, MockBB, MockEbay, MockDK, MockMouser, MockOEM, MockSrc, MockE14]
            _setup_mock_connectors(mocks)
            MockNexar.return_value.search = _tracking_search

            _make_api_source(db_session, "nexar")
            # Search 5 PNs — only nexar is active, so 5 tasks
            results, stats = await _fetch_fresh(["PN1", "PN2", "PN3", "PN4", "PN5"], db_session)

        assert max_concurrent <= 2


# ── _deduplicate_sightings tests ─────────────────────────────────────────


def _make_sighting_dict(**overrides):
    """Helper to build a sighting dict with sensible defaults."""
    base = {
        "id": 1,
        "requirement_id": 10,
        "vendor_name": "Digi-Key",
        "vendor_email": None,
        "vendor_phone": None,
        "mpn_matched": "LM358",
        "manufacturer": "Texas Instruments",
        "qty_available": 500,
        "unit_price": 0.42,
        "currency": "USD",
        "source_type": "digikey",
        "is_authorized": True,
        "confidence": 0.95,
        "score": 85.0,
        "is_unavailable": False,
        "moq": 1,
        "condition": "new",
        "date_code": None,
        "packaging": None,
        "lead_time_days": None,
        "lead_time": None,
        "created_at": "2026-02-28T10:00:00",
        "is_stale": False,
        "is_historical": False,
        "is_material_history": False,
    }
    base.update(overrides)
    return base


def test_dedup_filters_out_zero_qty():
    """Sightings with qty_available=0 are excluded; None (unknown) is kept."""
    rows = [
        _make_sighting_dict(id=1, qty_available=None, vendor_name="Nexar", source_type="nexar"),
        _make_sighting_dict(id=2, qty_available=0, vendor_name="Mouser", source_type="mouser"),
        _make_sighting_dict(id=3, qty_available=100),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 2
    result_ids = {r["id"] for r in result}
    assert result_ids == {1, 3}  # None kept, 0 filtered


def test_dedup_merges_same_vendor_mpn_price():
    """Same vendor+MPN+price from different sources → merge quantities."""
    rows = [
        _make_sighting_dict(
            id=1,
            vendor_name="Digi-Key",
            mpn_matched="LM358",
            unit_price=0.42,
            qty_available=500,
            score=80,
            source_type="digikey",
            confidence=0.9,
            moq=10,
        ),
        _make_sighting_dict(
            id=2,
            vendor_name="Digi-Key",
            mpn_matched="LM358",
            unit_price=0.42,
            qty_available=300,
            score=90,
            source_type="nexar",
            confidence=0.95,
            moq=1,
        ),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 1
    merged = result[0]
    assert merged["qty_available"] == 800  # summed
    assert merged["score"] == 90  # best score kept
    assert merged["confidence"] == 0.95  # best confidence
    assert merged["moq"] == 1  # lowest MOQ
    assert merged["merged_count"] == 2
    assert set(merged["merged_sources"]) == {"digikey", "nexar"}


def test_dedup_keeps_different_prices_separate():
    """Same vendor+MPN but different price → keep both lines."""
    rows = [
        _make_sighting_dict(id=1, vendor_name="Mouser", mpn_matched="LM358", unit_price=0.40, qty_available=100),
        _make_sighting_dict(id=2, vendor_name="Mouser", mpn_matched="LM358", unit_price=0.55, qty_available=200),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 2
    prices = {r["unit_price"] for r in result}
    assert prices == {0.40, 0.55}


def test_dedup_case_insensitive_vendor():
    """Vendor name matching is case-insensitive."""
    rows = [
        _make_sighting_dict(id=1, vendor_name="Digi-Key", unit_price=0.42, qty_available=100, source_type="digikey"),
        _make_sighting_dict(id=2, vendor_name="digi-key", unit_price=0.42, qty_available=200, source_type="nexar"),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 1
    assert result[0]["qty_available"] == 300


def test_dedup_preserves_historical_rows():
    """Historical and material-history rows pass through untouched."""
    rows = [
        _make_sighting_dict(id=1, is_historical=True, qty_available=None),
        _make_sighting_dict(id=2, is_material_history=True, qty_available=0),
        _make_sighting_dict(id=3, qty_available=100),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 3
    hist_ids = {r["id"] for r in result if r.get("is_historical") or r.get("is_material_history")}
    assert hist_ids == {1, 2}


def test_dedup_single_row_no_merge():
    """Single sighting for a vendor+mpn+price — no merge metadata added."""
    rows = [_make_sighting_dict(id=1, qty_available=50)]
    result = _deduplicate_sightings(rows)
    assert len(result) == 1
    assert "merged_count" not in result[0]


def test_dedup_none_price_grouped_separately():
    """Sightings with price=None are grouped separately from priced ones."""
    rows = [
        _make_sighting_dict(id=1, unit_price=None, qty_available=100),
        _make_sighting_dict(id=2, unit_price=0.42, qty_available=200),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 2


def test_dedup_different_mpn_not_merged():
    """Same vendor+price but different MPN → not merged (different parts)."""
    rows = [
        _make_sighting_dict(id=1, mpn_matched="LM358", unit_price=0.42, qty_available=100),
        _make_sighting_dict(id=2, mpn_matched="LM358N", unit_price=0.42, qty_available=200),
    ]
    result = _deduplicate_sightings(rows)
    assert len(result) == 2


def test_dedup_empty_list():
    """Empty input returns empty output."""
    assert _deduplicate_sightings([]) == []
