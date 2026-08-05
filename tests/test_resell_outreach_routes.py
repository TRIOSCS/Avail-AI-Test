"""test_resell_outreach_routes.py — Route/render tests for the Resell Outreach UI (Chunk
D).

Exercises the outreach endpoints with the TestClient:
  - submit creates ExcessOutreach via BOTH the manual-log and the email path (the
    email send is mocked at the source — send_batch_rfq / _find_sent_message);
  - the tracker renders rows + status + the "offered N · M responded · K bid" summary;
  - owner-gating (a non-owner gets 403).

The buyer-intelligence DISPLAY layer (ranked suggestions, no-contact rows, the
not-yet nudge strip) is PARKED (spec §5.3, W2.3) — its pins were dropped; the
parked-off state is pinned in tests/test_resell_trader_lane_parked.py.

All outreach endpoints are owner-gated (offering out is the list owner's action), so
the owner-path tests override require_user to the trader who owns the seeded list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus, ExcessOutreachStatus
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


# (test_buyer_panel_renders_ranked_suggestions / test_buyer_panel_overlap_flag /
# test_buyer_panel_no_contact_state deleted with the W2.3 buyer-intelligence park —
# spec §5.3: the panel's ranked-suggestion + no-contact rows are parked (empty)
# until a second trader user exists; the ranking/overlap SERVICE stays covered by
# tests/test_buyer_affinity_service.py, and the parked panel render is pinned in
# tests/test_resell_trader_lane_parked.py.)


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


# (test_not_yet_strip_renders / test_not_yet_strip_owner_gated deleted with the
# W2.3 buyer-intelligence park — spec §5.3: the not-yet-strip route registration
# was removed (nudge + auto My-Day task writes park together). The strip-ranking
# service stays covered by tests/test_buyer_affinity_service.py; the parked-off
# route state is pinned in tests/test_resell_trader_lane_parked.py.)


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


def test_log_bid_negative_quantity_400_not_500(client, db_session, trader_user, posted_list):
    """A negative quantity is rejected 400 at the route — never reaches the
    ExcessOfferLine @validates('quantity') ValueError as an unhandled 500 (finding #1).

    A negative qty is NOT None, so the old ``qty is None`` guard passed it through; the
    missing ``qty <= 0`` clause let it flow into a partial write + 500. No ExcessOffer may
    be created and the row stays ``sent``.
    """
    buyer = _reachable_buyer(db_session, "Fat Finger Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid",
            data={"mpn_raw": "GRM188R", "quantity": "-5", "unit_price": "0.88"},
        )
    finally:
        restore()
    assert resp.status_code == 400
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.SENT  # untouched — no partial write
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 0


@pytest.mark.parametrize(
    "bad_qty",
    ["3000000000", "1e30", "9999999999999"],
    ids=["int4_overflow", "exp_overflow", "huge_digits"],
)
def test_log_bid_overflow_quantity_400_not_500(client, db_session, trader_user, posted_list, bad_qty):
    """A quantity larger than the Postgres INT4 ceiling is rejected 400 at the route —
    never reaches the ExcessOfferLine INSERT as an unhandled NumericValueOutOfRange 500.

    ``_to_int`` parses "3000000000"/"1e30" into a huge positive int that passed the
    ``qty <= 0`` guard and only blew up on the INT4 column at flush time (masked by SQLite,
    which has no 32-bit bound). ``_to_int`` now returns None for an out-of-INT4-range value,
    so the ``qty is None`` guard rejects it up front. No ExcessOffer may be created and the
    row stays ``sent``.
    """
    buyer = _reachable_buyer(db_session, "Whale Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid",
            data={"mpn_raw": "GRM188R", "quantity": bad_qty, "unit_price": "0.88"},
        )
    finally:
        restore()
    assert resp.status_code == 400
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.SENT  # untouched — no partial write
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 0


@pytest.mark.parametrize(
    "bad_qty",
    ["inf", "1e999", "-inf", "nan"],
    ids=["infinity", "huge_exponent_to_inf", "negative_infinity", "nan"],
)
def test_log_bid_nonfinite_quantity_400_not_500(client, db_session, trader_user, posted_list, bad_qty):
    """A non-finite quantity ("inf"/"1e999"/"-inf"/"nan") is rejected 400 at the route —
    never raises an unhandled OverflowError out of the route before the guard runs.

    ``int(float("inf"))`` raises OverflowError (and ``int(float("nan"))`` a ValueError);
    ``_to_int`` now catches both and returns None, so the ``qty is None`` guard rejects the
    row instead of a 500 escaping to the global handler. No ExcessOffer is created.
    """
    buyer = _reachable_buyer(db_session, "Inf Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid",
            data={"mpn_raw": "GRM188R", "quantity": bad_qty, "unit_price": "0.88"},
        )
    finally:
        restore()
    assert resp.status_code == 400
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.SENT
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("500", 500),
        ("1,000", 1000),
        ("100.9", 100),
        ("", None),
        ("abc", None),
        ("3000000000", None),  # > INT4 max → None (finding: overflow 500)
        ("-3000000000", None),  # < INT4 min → None (underflow guard)
        ("2147483647", 2147483647),  # exactly INT4 max is fine
        ("inf", None),  # OverflowError inside int(float(...)) → None
        ("1e999", None),  # parses to inf → None
        ("-inf", None),
        ("nan", None),
    ],
)
def test_to_int_bounds_int4_and_swallows_nonfinite(raw, expected):
    """``_to_int`` returns None for anything that can't be stored in a Postgres INT4
    column (out of range, or a non-finite float that raises OverflowError) — the shared
    parser root-cause for the convert/log-bid/submit-offer quantity 400-not-500
    guards."""
    from app.routers.resell import _to_int

    assert _to_int(raw) == expected


def test_log_bid_double_submit_creates_no_duplicate_offer(client, db_session, trader_user, posted_list):
    """A replayed/duplicated Log-bid POST on a now-BID row records NO second ExcessOffer
    (finding #5/#9/#10).

    The first submit advances the manual row ``sent`` → ``bid`` and links one inbound
    offer. A second submit (double-click / racing re-render) must be idempotent: the
    status stays ``bid`` AND ``_link_inbound_offer`` does NOT run again, so the buyer's
    single manual bid is never inflated to two offers.
    """
    buyer = _reachable_buyer(db_session, "Replay Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="teams")
    restore = _own(db_session, None, trader_user)
    try:
        payload = {"mpn_raw": "GRM188R", "quantity": "500", "unit_price": "0.88"}
        first = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid", data=payload)
        second = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-bid", data=payload)
    finally:
        restore()
    assert first.status_code == 200 and second.status_code == 200
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.BID
    # Exactly ONE inbound offer — the replay did not create a second.
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 1


def test_log_bid_on_declined_row_creates_no_offer(client, db_session, trader_user, posted_list):
    """Log-bid on an already-DECLINED terminal row creates NO ExcessOffer and leaves the
    row ``declined`` (finding #9/#10 — the named 'terminal-row log-bid' case).

    ``record_manual_response`` protects the status from terminal regression; the offer
    link must be gated on the same terminal check, or a declined row silently sprouts an
    inbound bid that reads ``declined`` in the tracker.
    """
    buyer = _reachable_buyer(db_session, "Declined Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(
        db_session, posted_list, trader_user, buyer, channel="marketplace", status=ExcessOutreachStatus.DECLINED
    )
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
    assert row.status == ExcessOutreachStatus.DECLINED  # not regressed to bid
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id).count() == 0


def test_manual_log_bid_form_uses_honest_toast_copy(client, db_session, trader_user, posted_list):
    """The manual Log-bid modal's success toast reads honestly ('Bid logged') — NOT the
    email-thread copy 'Offer created from reply' (finding #7).

    The reply viewer is reused for the manual modal (manual=True); its convert form's
    after-request toast must reflect the manual framing the template itself renders.
    """
    buyer = _reachable_buyer(db_session, "Toast Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="phone")
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.get(f"/v2/partials/resell/{posted_list.id}/outreach/{row.id}/log-bid-form")
    finally:
        restore()
    assert resp.status_code == 200
    assert "Bid logged" in resp.text
    assert "Offer created from reply" not in resp.text


def test_manual_log_rejects_email_channel_row_with_thread(client, db_session, trader_user, posted_list):
    """An EMAIL-channel row that HAS a thread (graph_conversation_id set) must use the
    reply viewer, not the manual-log route → 409."""
    buyer = _reachable_buyer(db_session, "Email Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="email")
    row.graph_conversation_id = "conv-thread-1"
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-response")
    finally:
        restore()
    assert resp.status_code == 409


def test_manual_log_allows_degraded_email_row_without_thread(client, db_session, trader_user, posted_list):
    """Finding B3: a DEGRADED email row (status sent, no graph_conversation_id ever
    captured) has no thread to view — the reply viewer 404s it too — so the manual
    log-response/log-bid path is its one remaining outcome-logging route and must be
    ALLOWED, not 409."""
    buyer = _reachable_buyer(db_session, "Degraded Email Buyer", engagement=10.0, commodity=_CAP)
    row = _manual_outreach(db_session, posted_list, trader_user, buyer, channel="email")
    assert row.graph_conversation_id is None  # the degraded shape
    restore = _own(db_session, None, trader_user)
    try:
        resp = client.post(f"/api/resell/{posted_list.id}/outreach/{row.id}/log-response")
    finally:
        restore()
    assert resp.status_code == 200
    db_session.refresh(row)
    assert row.status == ExcessOutreachStatus.RESPONDED


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


# (test_no_contact_checkbox_enabled_for_manual_channel and
# test_no_contact_selection_purged_on_switch_back_to_email deleted with the W2.3
# buyer-intelligence park — spec §5.3: the no-contact rows no longer render (the
# panel's suggestion context is empty), so their channel-gating markup is
# unreachable. The finding-#6 setChannel purge wiring stays in the template and
# stays pinned by the parked panel render in
# tests/test_resell_trader_lane_parked.py.)


# ── W1.17 — keys-off outreach: manual log needs no M365 token ────────────────
# The route-level Depends(require_fresh_token) 401'd EVERY channel, but only the
# email channel actually sends via Graph. The token is now acquired in-branch for
# email only; a manual/phone log must work with no token, and a keys-off email
# submit gets an honest 409 (M365 not connected), not a login-bounce 401.


def test_submit_outreach_manual_channel_needs_no_token(client, db_session, trader_user, posted_list):
    """A phone-channel log succeeds with NO fresh-token override in place at all.

    Pops the conftest require_fresh_token override for the request, so a route-level
    Depends(require_fresh_token) would run for real and 401 (no session user) — the
    regression this guards: manual logging must never demand an M365 token.
    """
    from app.dependencies import require_fresh_token
    from app.main import app

    buyer = _reachable_buyer(db_session, "Keysoff Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    saved_override = app.dependency_overrides.pop(require_fresh_token, None)
    try:
        resp = client.post(
            f"/api/resell/{posted_list.id}/outreach",
            data={
                "vendor_card_ids": str(buyer.id),
                "scope": "whole_list",
                "channel": "phone",
                "notes": "logged keys-off",
            },
        )
        assert resp.status_code == 200
        rows = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).all()
        assert len(rows) == 1
        assert rows[0].channel == "phone"
    finally:
        if saved_override is not None:
            app.dependency_overrides[require_fresh_token] = saved_override
        restore()


def test_submit_outreach_email_keys_off_honest_409(client, db_session, trader_user, posted_list):
    """A keys-off EMAIL submit returns the honest 409 (M365 not connected), no rows.

    Simulates production keys-off: TESTING unset for the request (so the in-branch
    acquisition actually runs) and require_fresh_token raising its real 401. The route
    must rewrite that to the app's honest 409 (toast-surfaced app-wide by the global
    handler) instead of bouncing a logged-in user to login — and must write NO
    tracker rows.
    """
    import os as _os

    from fastapi import HTTPException as _HTTPException

    buyer = _reachable_buyer(db_session, "Keysoff Email Buyer", engagement=10.0, commodity=_CAP)
    db_session.commit()
    restore = _own(db_session, None, trader_user)
    token_mock = AsyncMock(side_effect=_HTTPException(401, "No access token — please log in"))
    try:
        with (
            patch.dict(_os.environ, {"TESTING": "0"}),
            patch("app.routers.resell.require_fresh_token", new=token_mock),
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
        assert resp.status_code == 409
        assert "Microsoft 365" in resp.json()["error"]
        token_mock.assert_awaited_once()
        rows = db_session.query(ExcessOutreach).filter_by(excess_list_id=posted_list.id).all()
        assert rows == []
    finally:
        restore()
