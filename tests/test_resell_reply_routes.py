"""test_resell_reply_routes.py — Route/render + helper tests for RS-4 reply tracking.

Covers the new owner-gated reply surface on resell detail:
  - ``_replies_context`` joins VendorResponse ↔ ExcessOutreach on graph_conversation_id
    (newest-first, threads without a conversation id excluded);
  - the reply-viewer route (403 non-owner, 404 when no thread, 200 renders the thread);
  - the convert-to-offer route (creates a matched inbound ExcessOffer + advances the
    outreach to ``bid``; owner-gated).

The client fixture authenticates as ``test_user``; the happy-path list is owned by that
user, and non-owner cases seed the list under a different owner.
Called by: pytest. Depends on: app.routers.resell, tests.conftest.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.models import Company, ExcessList, ExcessOutreach, User, VendorCard, VendorResponse
from app.models.excess import ExcessLineItem, ExcessOffer, ExcessOfferLine
from app.routers.resell import _replies_context
from app.utils.normalization import normalize_mpn_key

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def other_owner(db_session: Session) -> User:
    u = User(email="rs4-other@trioscs.com", name="RS4 Other", role="trader", azure_id="rs4-other")
    db_session.add(u)
    db_session.commit()
    db_session.refresh(u)
    return u


@pytest.fixture()
def buyer_card(db_session: Session) -> VendorCard:
    vc = VendorCard(normalized_name="buyer two", display_name="Buyer Two", emails=["sales@buyertwo.com"])
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    return vc


def _list(db: Session, owner: User) -> ExcessList:
    co = Company(name="RS4 Seller")
    db.add(co)
    db.flush()
    el = ExcessList(company_id=co.id, owner_id=owner.id, title="RS4 Routes Excess", status="open")
    db.add(el)
    db.commit()
    db.refresh(el)
    return el


def _outreach(db, el, card, owner, *, conv="conv-r", msg="msg-r", status="responded") -> ExcessOutreach:
    row = ExcessOutreach(
        excess_list_id=el.id,
        target_vendor_card_id=card.id,
        submitted_by=owner.id,
        channel="email",
        status=status,
        graph_conversation_id=conv,
        graph_message_id=msg,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _reply(db, conv, *, received_at, body="We'll take them.", email="sales@buyertwo.com") -> VendorResponse:
    vr = VendorResponse(
        vendor_name="Buyer Two",
        vendor_email=email,
        subject="RE: excess",
        body=body,
        graph_conversation_id=conv,
        received_at=received_at,
        status="matched",
    )
    db.add(vr)
    db.commit()
    db.refresh(vr)
    return vr


# ── _replies_context (test 7) ────────────────────────────────────────


class TestRepliesContext:
    def test_joins_and_orders_newest_first(self, db_session: Session, test_user: User, buyer_card: VendorCard):
        el = _list(db_session, test_user)
        a = _outreach(db_session, el, buyer_card, test_user, conv="cA", msg="mA")
        _outreach(db_session, el, buyer_card, test_user, conv="cB", msg="mB")
        # An outreach with NO conversation id must be excluded from the map.
        _outreach(db_session, el, buyer_card, test_user, conv=None, msg="mC")

        now = datetime.now(timezone.utc)
        older = _reply(db_session, "cA", received_at=now - timedelta(hours=2), body="first")
        newer = _reply(db_session, "cA", received_at=now - timedelta(hours=1), body="second")
        _reply(db_session, "cB", received_at=now, body="onB")

        ctx = _replies_context(db_session, el)

        assert set(ctx.keys()) == {"cA", "cB"}  # conv=None excluded
        assert ctx["cA"]["outreach"].id == a.id
        # Newest-first ordering within a conversation.
        assert [r.id for r in ctx["cA"]["replies"]] == [newer.id, older.id]
        assert len(ctx["cB"]["replies"]) == 1


# ── reply-viewer route (test 8) ──────────────────────────────────────


class TestReplyViewerRoute:
    def test_renders_thread_for_owner(self, client, db_session: Session, test_user: User, buyer_card: VendorCard):
        el = _list(db_session, test_user)
        row = _outreach(db_session, el, buyer_card, test_user)
        _reply(db_session, "conv-r", received_at=datetime.now(timezone.utc), body="Yes please")

        resp = client.get(f"/v2/partials/resell/{el.id}/outreach/{row.id}/reply")
        assert resp.status_code == 200
        assert "Yes please" in resp.text
        assert "Convert reply to an offer" in resp.text

    def test_non_owner_forbidden(self, client, db_session: Session, other_owner: User, buyer_card: VendorCard):
        el = _list(db_session, other_owner)  # owned by someone else
        row = _outreach(db_session, el, buyer_card, other_owner)
        resp = client.get(f"/v2/partials/resell/{el.id}/outreach/{row.id}/reply")
        assert resp.status_code == 403

    def test_no_thread_not_found(self, client, db_session: Session, test_user: User, buyer_card: VendorCard):
        el = _list(db_session, test_user)
        row = _outreach(db_session, el, buyer_card, test_user, conv=None, msg=None)
        resp = client.get(f"/v2/partials/resell/{el.id}/outreach/{row.id}/reply")
        assert resp.status_code == 404


# ── convert-to-offer route (tests 9-10) ──────────────────────────────


class TestConvertToOfferRoute:
    def test_creates_matched_offer_and_advances_to_bid(
        self, client, db_session: Session, test_user: User, buyer_card: VendorCard
    ):
        el = _list(db_session, test_user)
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number="LM358N",
                normalized_part_number=normalize_mpn_key("LM358N"),
                quantity=500,
            )
        )
        db_session.commit()
        row = _outreach(db_session, el, buyer_card, test_user)

        resp = client.post(
            f"/api/resell/{el.id}/outreach/{row.id}/offer",
            data={"mpn_raw": "LM358N", "quantity": "500", "unit_price": "1.25"},
        )
        assert resp.status_code == 200

        offers = db_session.query(ExcessOffer).filter(ExcessOffer.excess_list_id == el.id).all()
        assert len(offers) == 1
        assert offers[0].offerer_vendor_card_id == buyer_card.id

        line = db_session.query(ExcessOfferLine).filter(ExcessOfferLine.offer_id == offers[0].id).one()
        assert line.match_status == "matched"

        db_session.refresh(row)
        assert row.status == "bid"

    def test_missing_fields_rejected(self, client, db_session: Session, test_user: User, buyer_card: VendorCard):
        el = _list(db_session, test_user)
        row = _outreach(db_session, el, buyer_card, test_user)
        resp = client.post(
            f"/api/resell/{el.id}/outreach/{row.id}/offer",
            data={"mpn_raw": "", "quantity": ""},
        )
        assert resp.status_code == 400

    def test_owner_gated(self, client, db_session: Session, other_owner: User, buyer_card: VendorCard):
        el = _list(db_session, other_owner)  # owned by someone else
        row = _outreach(db_session, el, buyer_card, other_owner)
        resp = client.post(
            f"/api/resell/{el.id}/outreach/{row.id}/offer",
            data={"mpn_raw": "LM358N", "quantity": "10"},
        )
        assert resp.status_code == 403
