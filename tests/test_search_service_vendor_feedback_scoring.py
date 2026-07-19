"""tests/test_search_service_vendor_feedback_scoring.py — Vendor feedback adjustment
wired into live sighting scoring.

Covers app.search_service._save_sightings applying
app.services.sourcing_leads.get_vendor_feedback_adjustment to the vendor trust score
fed into score_sighting / score_sighting_v2 (PR #760 follow-up).

Called by: pytest
Depends on: app.search_service._save_sightings, app.services.sourcing_leads
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User, VendorCard
from app.search_service import _save_sightings
from app.services.sourcing_leads import update_lead_status, upsert_lead_from_sighting
from app.vendor_utils import normalize_vendor_name

# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session) -> User:
    u = User(
        email="feedback-scoring-test@trioscs.com",
        name="Feedback Scoring Test",
        role="buyer",
        azure_id="feedback-scoring-test-001",
        created_at=datetime.now(UTC),
    )
    db.add(u)
    db.flush()
    return u


def _make_requisition(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="FEEDBACK-SCORING-REQ",
        customer_name="Test Co",
        status="open",
        created_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(r)
    db.flush()
    return r


def _make_requirement(db: Session, requisition: Requisition, mpn: str = "LM317T") -> Requirement:
    req = Requirement(
        requisition_id=requisition.id,
        primary_mpn=mpn,
        target_qty=100,
        created_at=datetime.now(UTC),
    )
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_vendor_card(db: Session, name: str, vendor_score: float = 75.0) -> VendorCard:
    vc = VendorCard(
        normalized_name=normalize_vendor_name(name),
        display_name=name.title(),
        vendor_score=vendor_score,
        created_at=datetime.now(UTC),
    )
    db.add(vc)
    db.flush()
    return vc


def _give_feedback(db: Session, req: Requirement, vendor_name: str, mpn: str, status: str, count: int = 1) -> None:
    """Record `count` buyer feedback events of `status` against `vendor_name`'s lead."""
    sighting = Sighting(
        requirement_id=req.id,
        vendor_name=vendor_name,
        vendor_name_normalized=normalize_vendor_name(vendor_name),
        mpn_matched=mpn,
        source_type="brokerbin",
        unit_price=1.0,
        qty_available=100,
        created_at=datetime.now(UTC),
    )
    db.add(sighting)
    db.commit()
    lead = upsert_lead_from_sighting(db, req, sighting)
    db.commit()
    for _ in range(count):
        update_lead_status(db, lead.id, status)


def _fresh_hit(vendor_name: str, mpn: str = "LM317T") -> dict:
    return {
        "vendor_name": vendor_name,
        "mpn_matched": mpn,
        "qty_available": 100,
        "unit_price": 1.0,
        "currency": "USD",
        "source_type": "nexar",
        "is_authorized": False,
        "confidence": 3,
    }


# ── Tests ────────────────────────────────────────────────────────────────


class TestVendorFeedbackAdjustmentWiredIntoScoring:
    def test_bad_lead_heavy_vendor_scores_lower_than_clean_vendor(self, db_session: Session):
        """Identical listings from a bad_lead-heavy vendor and an equally-rated clean
        vendor must NOT score the same — the feedback penalty must actually move the
        trust factor and, with it, the final weighted score."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        bad_name = "Bad Feedback Vendor"
        clean_name = "Clean Vendor Electronics"
        _make_vendor_card(db_session, bad_name, vendor_score=75.0)
        _make_vendor_card(db_session, clean_name, vendor_score=75.0)
        _give_feedback(db_session, req, bad_name, "LM317T", "bad_lead", count=3)

        fresh = [_fresh_hit(bad_name), _fresh_hit(clean_name)]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})

        by_vendor = {s.vendor_name: s for s in result}
        bad_s = by_vendor[bad_name]
        clean_s = by_vendor[clean_name]
        # Clean vendor has no feedback history, so trust == its raw vendor_score.
        assert clean_s.score_components["trust"] == 75.0
        # Bad vendor's trust must be measurably reduced by the feedback penalty
        # (3x bad_lead, -6.0 each before decay) — a real value drop, not a no-op.
        assert bad_s.score_components["trust"] < 75.0
        assert bad_s.score_components["trust"] <= 75.0 - 15.0  # ~3 fresh bad_lead events, minimal decay
        assert bad_s.score_components["trust"] < clean_s.score_components["trust"]
        assert bad_s.score < clean_s.score

    def test_do_not_contact_floors_trust_but_does_not_drop_sighting(self, db_session: Session):
        """A do_not_contact vendor's sighting is floor-scored on trust (<=15), not
        dropped from the results — the buyer still sees the listing, deprioritized."""
        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        dnc_name = "Do Not Contact Vendor"
        _make_vendor_card(db_session, dnc_name, vendor_score=90.0)
        _give_feedback(db_session, req, dnc_name, "LM317T", "do_not_contact", count=1)

        fresh = [_fresh_hit(dnc_name, mpn="LM7805")]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})

        assert len(result) == 1  # sighting still surfaces, not dropped
        assert result[0].vendor_name == dnc_name
        assert result[0].score_components["trust"] <= 15.0

    def test_no_extra_feedback_query_per_sighting(self, db_session: Session, monkeypatch):
        """ONE get_vendor_feedback_adjustment call per DISTINCT vendor_card in the save,
        not one per sighting — 5 sightings across 2 vendors must issue 2 calls."""
        import app.search_service as search_service

        user = _make_user(db_session)
        reqn = _make_requisition(db_session, user)
        req = _make_requirement(db_session, reqn)

        vendor_a = _make_vendor_card(db_session, "Vendor A Electronics")
        vendor_b = _make_vendor_card(db_session, "Vendor B Electronics")
        db_session.commit()

        calls: list[int | None] = []
        original = search_service.get_vendor_feedback_adjustment

        def _counting(db, vendor_card_id):
            calls.append(vendor_card_id)
            return original(db, vendor_card_id)

        monkeypatch.setattr(search_service, "get_vendor_feedback_adjustment", _counting)

        fresh = [
            _fresh_hit("Vendor A Electronics", mpn="LM317T"),
            _fresh_hit("Vendor A Electronics", mpn="LM7805"),
            _fresh_hit("Vendor B Electronics", mpn="LM317T"),
            _fresh_hit("Vendor B Electronics", mpn="LM7805"),
            _fresh_hit("Vendor A Electronics", mpn="NE555P"),
        ]
        result = _save_sightings(fresh, req, db_session, succeeded_sources={"nexar"})

        assert len(result) == 5
        assert len(calls) == 2, f"expected exactly 2 feedback calls (1 per distinct vendor), got {len(calls)}: {calls}"
        assert set(calls) == {vendor_a.id, vendor_b.id}
