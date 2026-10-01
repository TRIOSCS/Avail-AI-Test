"""tests/test_part_report_service.py — Tests for app/services/part_report_service.py.

Covers the substitute parser, part resolution, the per-vendor posting history (grouping,
mirror-row exclusion, substitute coverage), the offer list + vendor count, and who-to-call
(vendor-card contacts first, posting contacts as fallback, unreachable vendors dropped).

Called by: pytest
Depends on: conftest db_session, MaterialCard, Sighting, Offer, VendorCard, VendorContact.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Sighting
from app.models.vendors import VendorCard, VendorContact
from app.services import part_report_service as svc

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _card(db: Session, key: str, display: str, **attrs) -> MaterialCard:
    card = MaterialCard(normalized_mpn=key, display_mpn=display, **attrs)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def _sighting(db: Session, card: MaterialCard, vendor: str, *, age_hours: int = 0, **attrs) -> Sighting:
    row = Sighting(
        material_card_id=card.id,
        vendor_name=vendor,
        mpn_matched=attrs.pop("mpn_matched", card.display_mpn),
        created_at=NOW - timedelta(hours=age_hours),
        **attrs,
    )
    db.add(row)
    db.commit()
    return row


# ── parse_substitutes / resolve_parts ─────────────────────────────────────


def test_parse_substitutes_splits_dedupes_and_caps():
    subs = svc.parse_substitutes("lm317, LM317AT; lm317t  lm338t LM350T", "LM317T")
    # Primary dropped, display-normalized, capped at MAX_SUBSTITUTES (3).
    assert subs == ["LM317", "LM317AT", "LM338T"]


def test_parse_substitutes_empty_and_duplicates():
    assert svc.parse_substitutes(None, "LM317T") == []
    assert svc.parse_substitutes("  ", "LM317T") == []
    assert svc.parse_substitutes("lm-317t, LM317T", "LM317T") == []  # both are the primary


def test_resolve_parts_marks_primary_and_attaches_cards(db_session: Session):
    card = _card(db_session, "lm317t", "LM317T")
    parts = svc.resolve_parts(db_session, "lm-317t", ["LM317AT"])
    assert [p.display for p in parts] == ["LM-317T", "LM317AT"]
    assert parts[0].is_primary and not parts[1].is_primary
    assert parts[0].card is not None and parts[0].card.id == card.id
    assert parts[1].card is None


# ── posted_before ─────────────────────────────────────────────────────────


def test_posted_before_groups_rows_per_vendor_latest_first(db_session: Session):
    card = _card(db_session, "lm317t", "LM317T")
    _sighting(db_session, card, "Arrow Electronics", age_hours=48, source_type="brokerbin", unit_price=Decimal("0.90"))
    _sighting(
        db_session,
        card,
        "Arrow Electronics, Inc.",
        age_hours=2,
        source_type="nexar",
        unit_price=Decimal("0.84"),
        qty_available=1200,
        vendor_email="sales@arrow.com",
    )
    _sighting(db_session, card, "Mouser", age_hours=24, source_type="mouser", is_authorized=True)
    # Resell-mirror rows are synthetic and must never show as vendor history.
    _sighting(db_session, card, "Customer Excess Co", age_hours=1, source_type="customer_excess")

    parts = svc.resolve_parts(db_session, "LM317T", [])
    rows = svc.posted_before(db_session, parts)

    assert [r.vendor_name for r in rows] == ["Arrow Electronics, Inc.", "Mouser"]
    arrow = rows[0]
    assert arrow.times_seen == 2
    assert arrow.sources == ["nexar", "brokerbin"]
    assert arrow.last_price == Decimal("0.84") and arrow.last_qty == 1200
    assert arrow.vendor_email == "sales@arrow.com"
    assert arrow.last_seen == NOW - timedelta(hours=2)
    assert arrow.first_seen == NOW - timedelta(hours=48)
    assert rows[1].is_authorized is True


def test_posted_before_includes_substitute_cards(db_session: Session):
    primary = _card(db_session, "lm317t", "LM317T")
    sub = _card(db_session, "lm317at", "LM317AT")
    _sighting(db_session, primary, "Arrow", age_hours=5)
    _sighting(db_session, sub, "Avnet", age_hours=1, source_type="brokerbin")

    parts = svc.resolve_parts(db_session, "LM317T", ["LM317AT"])
    rows = svc.posted_before(db_session, parts)
    assert [(r.vendor_name, r.part) for r in rows] == [("Avnet", "LM317AT"), ("Arrow", "LM317T")]


def test_posted_before_without_cards_is_empty(db_session: Session):
    parts = svc.resolve_parts(db_session, "ZZ-NOPE-1", [])
    assert svc.posted_before(db_session, parts) == []


# ── offers_for_parts ──────────────────────────────────────────────────────


def test_offers_for_parts_latest_first_and_vendor_count(db_session: Session):
    card = _card(db_session, "lm317t", "LM317T")
    for i, (vendor, price) in enumerate([("Avnet", "4.10"), ("Arrow", "3.90"), ("Avnet Inc", "4.00")]):
        db_session.add(
            Offer(
                material_card_id=card.id,
                vendor_name=vendor,
                mpn="LM317T",
                unit_price=Decimal(price),
                qty_available=100 + i,
                status="active",
                source="email_parsed",
                created_at=NOW - timedelta(days=i),
            )
        )
    db_session.commit()

    parts = svc.resolve_parts(db_session, "LM317T", [])
    offers = svc.offers_for_parts(db_session, parts)
    assert [o.vendor_name for o in offers] == ["Avnet", "Arrow", "Avnet Inc"]
    assert svc.distinct_vendor_count(offers) == 2  # "Avnet" and "Avnet Inc" normalize together


# ── who_to_call ───────────────────────────────────────────────────────────


def test_who_to_call_prefers_vendor_card_contacts_then_posting_contacts(db_session: Session):
    card = _card(db_session, "lm317t", "LM317T")
    arrow = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["info@arrow.com"],
        phones=["+1 800 000 0000"],
        total_wins=3,
        total_outreach=10,
        last_contact_at=NOW - timedelta(days=3),
    )
    db_session.add(arrow)
    db_session.flush()
    db_session.add(
        VendorContact(
            vendor_card_id=arrow.id,
            full_name="Sam Lee",
            title="Account Manager",
            email="sam@arrow.com",
            phone="+1 714 555 1212",
            source="manual",
            is_primary=True,
        )
    )
    db_session.add(
        VendorContact(
            vendor_card_id=arrow.id, full_name="Old Rep", email="old@arrow.com", source="manual", relationship_score=99
        )
    )
    # A vendor card with no reachable channel at all → dropped.
    db_session.add(VendorCard(normalized_name="ghost supply", display_name="Ghost Supply"))
    db_session.commit()

    _sighting(db_session, card, "Arrow Electronics", age_hours=2)
    _sighting(db_session, card, "Ghost Supply", age_hours=3)
    _sighting(db_session, card, "Broker Only", age_hours=4, vendor_phone="555-0100")
    _sighting(db_session, card, "Silent Broker", age_hours=5)  # no card, no contact → dropped
    db_session.add(
        Offer(material_card_id=card.id, vendor_name="Arrow Electronics", mpn="LM317T", status="active", created_at=NOW)
    )
    db_session.commit()

    parts = svc.resolve_parts(db_session, "LM317T", [])
    postings = svc.posted_before(db_session, parts)
    offers = svc.offers_for_parts(db_session, parts)
    targets = svc.who_to_call(db_session, postings, offers)

    assert [t.vendor_name for t in targets] == ["Arrow Electronics", "Broker Only"]
    arrow_t = targets[0]
    assert arrow_t.vendor_card_id == arrow.id
    assert arrow_t.contact_name == "Sam Lee"  # the primary contact wins over the higher score
    assert arrow_t.email == "sam@arrow.com" and arrow_t.phone == "+1 714 555 1212"
    assert arrow_t.total_wins == 3 and arrow_t.basis == "posting + offer"
    broker_t = targets[1]
    assert broker_t.vendor_card_id is None
    assert broker_t.phone == "555-0100" and broker_t.email is None
    assert broker_t.basis == "posting"


def test_who_to_call_orders_cards_by_wins_then_recency(db_session: Session):
    card = _card(db_session, "lm317t", "LM317T")
    for name, wins, days_ago in [("Alpha", 0, 1), ("Beta", 2, 30), ("Gamma", 0, 10)]:
        db_session.add(
            VendorCard(
                normalized_name=name.lower(),
                display_name=name,
                emails=[f"sales@{name.lower()}.com"],
                total_wins=wins,
                last_contact_at=NOW - timedelta(days=days_ago),
            )
        )
        _sighting(db_session, card, name, age_hours=1)
    db_session.commit()

    parts = svc.resolve_parts(db_session, "LM317T", [])
    targets = svc.who_to_call(db_session, svc.posted_before(db_session, parts), [])
    assert [t.vendor_name for t in targets] == ["Beta", "Alpha", "Gamma"]


def test_who_to_call_flags_blacklisted_vendor(db_session: Session):
    card = _card(db_session, "lm317t", "LM317T")
    db_session.add(
        VendorCard(
            normalized_name="shady parts", display_name="Shady Parts", emails=["x@shady.io"], is_blacklisted=True
        )
    )
    db_session.commit()
    _sighting(db_session, card, "Shady Parts", age_hours=1)
    parts = svc.resolve_parts(db_session, "LM317T", [])
    targets = svc.who_to_call(db_session, svc.posted_before(db_session, parts), [])
    assert len(targets) == 1 and targets[0].is_blacklisted is True


def test_who_to_call_empty_inputs(db_session: Session):
    assert svc.who_to_call(db_session, [], []) == []
