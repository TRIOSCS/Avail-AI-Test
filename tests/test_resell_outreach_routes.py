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

from datetime import UTC, datetime
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
        created_at=datetime.now(UTC),
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
def draft_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A DRAFT list owned by the trader (not yet posted)."""
    el = ExcessList(
        title="D draft caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.DRAFT,
        total_line_items=0,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def posted_list(db_session: Session, trader_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list owned by the trader, with one capacitor line."""
    el = ExcessList(
        title="D surplus caps",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=1,
        created_at=datetime.now(UTC),
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
            sent_at=datetime.now(UTC),
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


def _customer_named_list(db_session, trader_user, test_company) -> ExcessList:
    """A posted list a trader named after the customer (the natural, leaky habit)."""
    el = ExcessList(
        title=f"{test_company.name} — surplus FPGAs Q3",
        company_id=test_company.id,
        owner_id=trader_user.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(
        ExcessLineItem(
            excess_list_id=el.id,
            part_number="XCVU9P-2FLGA2104I",
            normalized_part_number=normalize_mpn_key("XCVU9P-2FLGA2104I"),
            quantity=10,
        )
    )
    db_session.commit()
    return el


def test_outreach_subject_prefill_is_neutral(client, db_session, trader_user, test_company):
    """#11: the outreach email subject PREFILL must not embed the customer-named list
    title.

    The subject ships externally to the buyer, so embedding ``el.title`` (which traders
    write as the customer name) de-anonymizes the customer. The prefill is a neutral,
    part-count default instead; the owner can still edit it before sending.
    """
    import re

    el = _customer_named_list(db_session, trader_user, test_company)
    restore = _own(db_session, None, trader_user)
    try:
        body = client.get(f"/v2/partials/resell/{el.id}/offer-buyers-form").text
    finally:
        restore()

    m = re.search(r'name="subject"[^>]*value="([^"]*)"', body)
    assert m, "subject input not found in the outreach modal"
    subject_value = m.group(1)
    assert el.title not in subject_value, "customer-named title leaked into the outreach subject prefill"
    assert test_company.name not in subject_value
    assert subject_value.strip(), "a neutral default subject must be present"
    assert "Excess available" in subject_value  # neutral, part-count prefix


def test_outreach_activity_log_subject_omits_customer_title(client, db_session, trader_user, test_company):
    """#11: the internal outreach ActivityLog subject must not embed the customer-named
    list title.

    The log lands on the (shared) buyer vendor-card timeline, so the title would leak
    the customer to any OTHER trader viewing that buyer. It references the list
    neutrally by id.
    """
    from app.models import ActivityLog

    el = _customer_named_list(db_session, trader_user, test_company)
    buyer = _reachable_buyer(db_session, "Timeline Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{el.id}/outreach",
            data={"vendor_card_ids": str(buyer.id), "scope": "whole_list", "channel": "phone"},
        )
        assert resp.status_code == 200
    finally:
        restore()

    logs = db_session.query(ActivityLog).filter_by(excess_list_id=el.id).all()
    assert logs, "an outreach ActivityLog should have been written"
    subject = logs[0].subject or ""
    assert el.title not in subject, "customer title leaked into the outreach ActivityLog subject"
    assert test_company.name not in subject
    assert f"#{el.id}" in subject, "the log should still reference the list neutrally by id"


def test_submit_outreach_email_path(client, db_session, trader_user, posted_list):
    """The email channel enqueues 'sending' rows + a background send, returning at once.

    The send + per-buyer Graph lookups no longer run inline (they hung the modal for a
    multi-buyer send) — they are a background job (stubbed here so the response reflects
    the optimistic 'sending' state the modal sees). The finalization is covered by
    tests/test_resell_outreach_async.py::TestRunOutreachEmailSend.
    """
    from unittest.mock import MagicMock

    buyer = _reachable_buyer(db_session, "Email Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    send_mock = AsyncMock()
    run_stub = MagicMock()
    try:
        with (
            patch("app.email_service.send_batch_rfq", new=send_mock),
            patch("app.services.resell_outreach_service.run_outreach_email_send", new=run_stub),
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
        # The request path never ran the send loop — that is the background job's work.
        send_mock.assert_not_called()
        run_stub.assert_called_once()
        rows = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).all()
        assert len(rows) == 1
        assert rows[0].channel == "email"
        assert rows[0].status == ExcessOutreachStatus.SENDING
        assert rows[0].graph_conversation_id is None
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
                sent_at=datetime.now(UTC),
            ),
            ExcessOutreach(
                excess_list_id=posted_list.id,
                target_vendor_card_id=buyer_sent.id,
                submitted_by=trader_user.id,
                channel="phone",
                status=ExcessOutreachStatus.SENT,
                sent_at=datetime.now(UTC),
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


# ── Fix 1 — 422 on invalid channel ──────────────────────────────────


def test_submit_outreach_invalid_channel_422(client, db_session, trader_user, posted_list):
    """A bogus channel value is rejected with 422 (not an unhandled 500)."""
    buyer = _reachable_buyer(db_session, "Any Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach",
            data={
                "vendor_card_ids": str(buyer.id),
                "scope": "whole_list",
                "channel": "bogus_channel",
            },
        )
        assert resp.status_code == 422
    finally:
        restore()


# ── Fix 2 — 409 on draft list ────────────────────────────────────────


def test_submit_outreach_draft_list_409(client, db_session, trader_user, draft_list):
    """Submitting outreach on a DRAFT list is rejected with 409."""
    buyer = _reachable_buyer(db_session, "Draft Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{draft_list.id}/outreach",
            data={
                "vendor_card_ids": str(buyer.id),
                "scope": "whole_list",
                "channel": "phone",
            },
        )
        assert resp.status_code == 409
    finally:
        restore()


# NB: the former assert-200-only ``test_submit_outreach_posted_list_200`` was dropped as
# redundant assertion theater — the posted-list success path is covered with real
# outcome assertions by ``test_submit_outreach_log_path`` (rows/channel/target created)
# and ``test_submit_outreach_email_path`` (sending rows + background job dispatched).


# ── Task 5 (finding #12): manual-channel Log response / Log their bid ─────────
# A manual-channel (phone/teams/marketplace) outreach row is created at 'sent' with no
# graph_conversation_id, so the email reply-viewer/convert path (keyed on the thread) can
# never advance it — it was a dead-end. The owner can now log the outcome directly on the
# row: Log response (-> responded) or Log their bid (-> bid + an ExcessOffer via the SAME
# convert path an emailed bid uses). The no-contact checkbox is enabled for manual channels.


def _manual_outreach(db_session, el, owner, card, *, channel="phone", status=None):
    from app.constants import ExcessOutreachStatus

    row = ExcessOutreach(
        excess_list_id=el.id,
        submitted_by=owner.id,
        target_vendor_card_id=card.id,
        channel=channel,
        status=status or ExcessOutreachStatus.SENT,
        created_at=datetime.now(UTC),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_log_response_flips_manual_row_to_responded(client, db_session, trader_user, posted_list):
    buyer = _reachable_buyer(db_session, "Phone Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-response")
    finally:
        restore()
    assert resp.status_code == 200
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.RESPONDED
    # The returned partial is the tracker (shows the buyer + summary).
    assert "Phone Buyer" in resp.text


def test_log_bid_creates_offer_and_flips_bid(client, db_session, trader_user, posted_list):
    buyer = _reachable_buyer(db_session, "Teams Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="teams")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid",
            data={"mpn_raw": "GRM188R", "quantity": "500", "unit_price": "0.88"},
        )
    finally:
        restore()
    assert resp.status_code == 200
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.BID
    # The bid was recorded as a real inbound ExcessOffer scoped to the buyer card.
    offers = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).all()
    assert len(offers) == 1
    assert offers[0].offerer_vendor_card_id == buyer.id
    offer_line = db_session.query(ExcessOfferLine).filter_by(offer_id=offers[0].id).one()
    assert offer_line.unit_price == Decimal("0.88")
    # The matched line got its rollup recomputed (the salvaged bid owns it).
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).first()
    assert line.best_offer_id == offers[0].id


def test_log_bid_form_renders_convert_form(client, db_session, trader_user, posted_list):
    """The Log-bid modal reuses the convert-to-offer form, pointed at the manual log-bid
    route."""
    buyer = _reachable_buyer(db_session, "Marketplace Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="marketplace")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/{row.id}/log-bid-form")
    finally:
        restore()
    assert resp.status_code == 200
    assert f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid" in resp.text
    assert 'name="mpn_raw"' in resp.text  # the reused convert line form


def test_log_bid_never_regresses_terminal_row(client, db_session, trader_user, posted_list):
    """A row already at 'bid' is not regressed by a stray log-response."""
    buyer = _reachable_buyer(db_session, "Done Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(
        db_session, posted_list, trader_user, buyer, channel="phone", status=ExcessOutreachStatus.BID
    )
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-response")
    finally:
        restore()
    assert resp.status_code == 200
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.BID  # not regressed to responded


def test_manual_log_rejects_email_channel_row(client, db_session, trader_user, posted_list):
    """An EMAIL-channel row must use the reply viewer, not the manual-log route →
    409."""
    buyer = _reachable_buyer(db_session, "Email Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="email")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-response")
    finally:
        restore()
    assert resp.status_code == 409


def test_log_response_owner_gated(client, db_session, trader_user, posted_list):
    """The default client user is not the owner → 403 (the row is untouched)."""
    buyer = _reachable_buyer(db_session, "Guard Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-response")
    assert resp.status_code == 403
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.SENT


def test_tracker_shows_log_actions_for_manual_sent_row(client, db_session, trader_user, posted_list):
    """A manual 'sent' row surfaces Log-response + Log-bid affordances in the
    tracker."""
    buyer = _reachable_buyer(db_session, "Log Actions Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    restore = _own(db_session, None, trader_user)
    try:
        body = client.get(f"/v2/partials/resell/{posted_list.id}/outreach").text
    finally:
        restore()
    assert f"/outreach/{row.id}/log-response" in body
    assert f"/outreach/{row.id}/log-bid-form" in body


def test_no_contact_checkbox_enabled_for_manual_channel(client, db_session, trader_user, posted_list):
    """The no-contact buyer checkbox is disabled ONLY for the email channel — manual
    channels (phone/teams/marketplace) can log a touch without an email on file."""
    no_contact = VendorCard(normalized_name="no email log buyer", display_name="No Email Log Buyer", emails=[])
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
        body = client.get(f"/v2/partials/resell/{posted_list.id}/offer-buyers-form").text
    finally:
        restore()
    # The checkbox is now Alpine-gated on the channel rather than hard-disabled.
    assert ":disabled=\"channel === 'email'\"" in body
