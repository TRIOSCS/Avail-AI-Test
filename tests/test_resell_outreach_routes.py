"""test_resell_outreach_routes.py — Route/render tests for the Resell Outreach UI (Chunk
D).

Exercises the NEW additive outreach endpoints with the TestClient:
  - the buyer panel renders ranked suggestions + the advisory overlap flag + the
    "no contact on file" state;
  - submit creates ExcessOutreach via BOTH the manual-log and the email path (the
    email send is mocked at the source — send_batch_rfq / _find_sent_message);
  - the tracker renders rows + status + the "offered N · M responded · K bid" summary;
  - the "usually-offered, not yet" strip renders;
  - owner-gating (a non-owner gets 403).

All outreach endpoints are owner-gated (offering out is the list owner's action), so
the owner-path tests override require_user to the trader who owns the seeded list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOfferStatus, ExcessOutreachStatus, OfferLineMatchStatus
from app.models import Company, User, VendorCard
from app.models.excess import (
    ExcessLineItem,
    ExcessList,
    ExcessOffer,
    ExcessOfferLine,
    ExcessOutreach,
)
from app.models.intelligence import MaterialCard
from app.models.vendors import VendorContact
from app.utils.normalization import normalize_mpn_key

_CAP = "capacitors"


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def trader_user(db_session: Session) -> User:
    """The list owner — a trader (can_post + owns the list = can offer it out)."""
    user = User(
        email="d-trader@trioscs.com",
        name="Dee Trader",
        role="trader",
        azure_id="d-azure-trader",
        m365_connected=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def teammate_user(db_session: Session) -> User:
    """A second trader — the source of the advisory overlap warning."""
    user = User(email="d-mate@trioscs.com", name="Dee Mate", role="trader", azure_id="d-azure-mate")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader, with one capacitor line."""
    el = ExcessList(
        title="D surplus caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=1,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(el)
    db_session.flush()
    mc = MaterialCard(normalized_mpn="grm188r", display_mpn="GRM188R", category=_CAP)
    db_session.add(mc)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="GRM188R",
            normalized_part_number=normalize_mpn_key("GRM188R"),
            quantity=1000,
            condition="New",
            material_card_id=mc.id,
            asking_price=Decimal("1.00"),
        )
    )
    db_session.commit()
    db_session.refresh(el)
    return el


def _reachable_buyer(
    db: Session, name: str, *, engagement: float | None = None, commodity: str | None = None
) -> VendorCard:
    """A buyer card with a resolvable VendorContact email (passes the RFQ reachability
    gate) and optional engagement + commodity tag — i.e. an actually-offerable buyer."""
    email = f"buy@{name.lower().replace(' ', '')}.com"
    vc = VendorCard(
        normalized_name=name.lower(),
        display_name=name,
        emails=[email],
        engagement_score=engagement,
        commodity_tags=[commodity] if commodity else None,
    )
    db.add(vc)
    db.flush()
    db.add(VendorContact(vendor_card_id=vc.id, email=email, full_name="Buyer", source="test"))
    db.flush()
    return vc


def _own(db_session, monkeypatch_app, user):
    """Override require_user to *user* (the owner).

    Returns a cleanup callable.
    """
    from app.dependencies import require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


# ── Buyer panel (offer-to-buyers form) ───────────────────────────────


def test_buyer_panel_renders_ranked_suggestions(client, db_session, trader_user, posted_list):
    """The buyer panel renders ranked suggestions for an offerable buyer."""
    _reachable_buyer(db_session, "Cap Buyer", engagement=50.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form")
        assert resp.status_code == 200
        body = resp.text
        assert "Offer to buyers" in body
        assert "Cap Buyer" in body  # ranked suggestion surfaced
        # Channel selector present (email default) + scope toggle.
        assert "Channel" in body and "email" in body
        assert "whole list" in body.lower()
    finally:
        restore()


def test_buyer_panel_overlap_flag(client, db_session, trader_user, teammate_user, posted_list):
    """A recent teammate touch surfaces the advisory overlap flag on the suggestion."""
    buyer = _reachable_buyer(db_session, "Overlap Buyer", engagement=50.0, commodity=_CAP)
    db_session.add(
        ExcessOutreach(
            excess_list_id=posted_list.id,
            target_vendor_card_id=buyer.id,
            submitted_by=teammate_user.id,  # a TEAMMATE, not the owner
            channel="phone",
            status=ExcessOutreachStatus.SENT,
            sent_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form")
        assert resp.status_code == 200
        body = resp.text
        # The advisory flag names the teammate (never blocks — advisory only).
        assert "Dee Mate" in body
        assert "already" in body.lower()
    finally:
        restore()


def test_buyer_panel_no_contact_state(client, db_session, trader_user, posted_list):
    """A buyer reachable only via no resolvable email surfaces the 'no contact' state.

    A card with offer history but NO VendorContact email is unreachable by the send
    path; the panel must still list it for a manual-log touch with a clear no-contact
    badge (mirrors the RFQ modal's no-email treatment).
    """
    # A buyer with WON offer history on this list's line but NO contact email.
    no_contact = VendorCard(normalized_name="no email buyer", display_name="No Email Buyer", emails=[])
    no_contact.commodity_tags = [_CAP]
    db_session.add(no_contact)
    db_session.flush()
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    offer = ExcessOffer(
        excess_list_id=posted_list.id,
        submitted_by=trader_user.id,
        offerer_vendor_card_id=no_contact.id,
        scope="per_line",
        status=ExcessOfferStatus.WON,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=line.id,
            mpn_raw=line.part_number,
            quantity=10,
            unit_price=Decimal("0.90"),
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form")
        assert resp.status_code == 200
        body = resp.text
        assert "No Email Buyer" in body
        assert "no contact on file" in body.lower()
    finally:
        restore()


def test_buyer_panel_owner_gated(client, db_session, posted_list):
    """A non-owner (the default buyer client user) cannot open the buyer panel → 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form")
    assert resp.status_code == 403


# ── Submit — manual-log path ─────────────────────────────────────────


def test_submit_outreach_log_path(client, db_session, trader_user, posted_list):
    """Submitting a phone/manual outreach creates ExcessOutreach rows + returns the
    tracker."""
    buyer = _reachable_buyer(db_session, "Log Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach",
            data={
                "vendor_card_ids": str(buyer.id),
                "scope": "whole_list",
                "channel": "phone",
                "notes": "left a voicemail",
            },
        )
        assert resp.status_code == 200
        rows = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).all()
        assert len(rows) == 1
        assert rows[0].channel == "phone"
        assert rows[0].target_vendor_card_id == buyer.id
        # The returned partial is the tracker (shows the buyer + the summary).
        assert "Log Buyer" in resp.text
    finally:
        restore()


def test_submit_outreach_email_path(client, db_session, trader_user, posted_list):
    """The email channel routes through submit_outreach_email (send mocked at
    source)."""
    buyer = _reachable_buyer(db_session, "Email Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    sent_payload = [{"vendor_email": buyer.emails[0], "status": "sent"}]
    try:
        with (
            patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=sent_payload)),
            patch(
                "app.email_service._find_sent_message", new=AsyncMock(return_value={"id": "m1", "conversationId": "c1"})
            ),
        ):
            resp = client.post(
                f"/api/resell/{posted_list.id}/outreach",
                data={
                    "vendor_card_ids": str(buyer.id),
                    "scope": "whole_list",
                    "channel": "email",
                    "subject": "Excess offer",
                    "body": "We have these parts available.",
                },
            )
        assert resp.status_code == 200
        rows = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).all()
        assert len(rows) == 1
        assert rows[0].channel == "email"
        assert rows[0].status == ExcessOutreachStatus.SENT
        assert rows[0].graph_conversation_id == "c1"
    finally:
        restore()


def test_submit_outreach_owner_gated(client, db_session, posted_list):
    """A non-owner cannot submit outreach → 403."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/outreach",
        data={"vendor_card_ids": "1", "scope": "whole_list", "channel": "phone"},
    )
    assert resp.status_code == 403


# ── Tracker ──────────────────────────────────────────────────────────


def test_tracker_renders_rows_and_summary(client, db_session, trader_user, posted_list):
    """The tracker renders one row per buyer touch + the 'offered N · M responded · K
    bid' summary."""
    buyer_bid = _reachable_buyer(db_session, "Bid Buyer", commodity=_CAP)
    buyer_sent = _reachable_buyer(db_session, "Sent Buyer", commodity=_CAP)
    db_session.add_all(
        [
            ExcessOutreach(
                excess_list_id=posted_list.id,
                target_vendor_card_id=buyer_bid.id,
                submitted_by=trader_user.id,
                channel="email",
                status=ExcessOutreachStatus.BID,
                sent_at=datetime.now(timezone.utc),
            ),
            ExcessOutreach(
                excess_list_id=posted_list.id,
                target_vendor_card_id=buyer_sent.id,
                submitted_by=trader_user.id,
                channel="phone",
                status=ExcessOutreachStatus.SENT,
                sent_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach")
        assert resp.status_code == 200
        body = resp.text
        assert "Bid Buyer" in body and "Sent Buyer" in body
        # Summary headline: 2 offered, 1 bid.
        assert "2" in body
        assert "offered" in body.lower()
        assert "bid" in body.lower()
    finally:
        restore()


def test_tracker_owner_gated(client, db_session, posted_list):
    """The tracker is the owner's private board → a non-owner gets 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach")
    assert resp.status_code == 403


def test_tracker_empty_state(client, db_session, trader_user, posted_list):
    """No outreach yet → an honest empty state, not a blank table."""
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach")
        assert resp.status_code == 200
        assert "Offer to buyers" in resp.text or "not offered" in resp.text.lower()
    finally:
        restore()


# ── Not-yet-offered nudge strip ──────────────────────────────────────


def test_not_yet_strip_renders(client, db_session, trader_user, posted_list):
    """A historical commodity buyer not yet offered this list surfaces in the nudge."""
    # A buyer with WON history in this commodity (on a PRIOR list) → historical signal.
    historical = _reachable_buyer(db_session, "Usually Buyer", engagement=60.0, commodity=_CAP)
    prior = ExcessList(
        title="Prior", company_id=posted_list.company_id, owner_id=trader_user.id, status=ExcessListStatus.AWARDED
    )
    db_session.add(prior)
    db_session.flush()
    prior_mc = MaterialCard(normalized_mpn="grm21b", display_mpn="GRM21B", category=_CAP)
    db_session.add(prior_mc)
    db_session.flush()
    prior_line = ExcessLineItem(
        excess_list_id=prior.id,
        part_number="GRM21B",
        quantity=10,
        material_card_id=prior_mc.id,
        asking_price=Decimal("1.0"),
    )
    db_session.add(prior_line)
    db_session.flush()
    offer = ExcessOffer(
        excess_list_id=prior.id,
        submitted_by=trader_user.id,
        offerer_vendor_card_id=historical.id,
        scope="per_line",
        status=ExcessOfferStatus.WON,
    )
    db_session.add(offer)
    db_session.flush()
    db_session.add(
        ExcessOfferLine(
            offer_id=offer.id,
            excess_line_item_id=prior_line.id,
            mpn_raw="GRM21B",
            quantity=10,
            unit_price=Decimal("0.9"),
            match_status=OfferLineMatchStatus.MATCHED,
        )
    )
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/not-yet-strip")
        assert resp.status_code == 200
        assert "Usually Buyer" in resp.text
    finally:
        restore()


def test_not_yet_strip_owner_gated(client, db_session, posted_list):
    """The nudge is owner-only → a non-owner gets 403."""
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/not-yet-strip")
    assert resp.status_code == 403


def test_detail_has_outreach_tab(client, db_session, trader_user, posted_list):
    """The detail panel exposes the Outreach tab + the Offer-to-buyers action
    (owner)."""
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}")
        assert resp.status_code == 200
        body = resp.text
        assert "Outreach" in body
        assert "offer-buyers-form" in body  # the action is wired
    finally:
        restore()
