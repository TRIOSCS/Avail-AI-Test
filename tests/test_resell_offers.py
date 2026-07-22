"""test_resell_offers.py — Inbound-offer submission + line MaterialCard resolve.

Covers the additive Resell offer-collection core (spec §"Offer collection"):
- ``ExcessLineItem.material_card_id`` resolved on the import/create path (reusing
  the canonical ``resolve_material_card``; nullable when the MPN won't resolve).
- ``submit_offer`` for both scopes:
    - ``take_all`` → one ExcessOffer (status open), lump price, NO lines.
    - ``per_line`` → ExcessOffer + one ExcessOfferLine per row, matched on part
      number only via ``normalize_mpn_key`` (price never affects matching):
      exactly-one match → matched; none → unmatched (QUEUED, never dropped);
      duplicate posting MPN → ambiguous.
- Guards: self-offer blocked (submitted_by == list.owner_id); non-can_offer blocked.

Called by: pytest
Depends on: app.services.excess_service, app.models.excess, tests.conftest
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.constants import (
    ExcessListStatus,
    ExcessOfferScope,
    ExcessOfferStatus,
    ExcessOutreachChannel,
    ExcessOutreachStatus,
    OfferLineMatchStatus,
)
from app.models import Company, MaterialCard, User, VendorCard
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine, ExcessOutreach
from app.routers.resell import _award_response_context, _offers_context, _outreach_tracker_context
from app.services.excess_service import (
    confirm_import,
    create_excess_list,
    import_line_items,
    submit_offer,
)
from app.utils.normalization import normalize_mpn_key
from tests.conftest import engine

_ = engine  # Ensure test DB tables are created


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_excess_crud.py fixture style)
# ---------------------------------------------------------------------------


def _make_company(db: Session, name: str = "Seller Corp") -> Company:
    co = Company(name=name)
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


def _make_user(db: Session, *, email: str, role: str = "trader") -> User:
    user = User(email=email, name=email.split("@")[0], role=role, azure_id=f"az-{email}")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_list_with_lines(db: Session, owner: User, company: Company, parts: list[str]) -> ExcessList:
    """Create a POSTED (open) ExcessList with one line per part (via import path so
    resolve fires).

    Posted, not draft: ``submit_offer`` rejects a non-posted list (finding #47), so every
    caller that submits an offer against a list built by this helper needs it already
    open. Callers that want a specific status (e.g. bid_out/awarded for late-offer tests)
    still override ``el.status`` after this returns.
    """
    el = create_excess_list(db, title="Excess", company_id=company.id, owner_id=owner.id)
    rows = [{"part_number": p, "quantity": "100"} for p in parts]
    import_line_items(db, el.id, rows)
    el.status = ExcessListStatus.OPEN
    db.commit()
    db.refresh(el)
    return el


# ---------------------------------------------------------------------------
# MaterialCard resolve on line create
# ---------------------------------------------------------------------------


def test_import_resolves_material_card_id(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner@test.com", role="sales")
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)

    import_line_items(db_session, el.id, [{"part_number": "LM358N", "quantity": "100"}])

    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    assert item.material_card_id is not None
    card = db_session.get(MaterialCard, item.material_card_id)
    assert card.normalized_mpn == "lm358n"


def test_confirm_import_resolves_material_card_id(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner2@test.com", role="sales")
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)

    confirm_import(db_session, el.id, [{"part_number": "MAX232", "quantity": 50}])

    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    assert item.material_card_id is not None
    # The line resolves to the ACTUAL MAX232 card (not just some non-null row): confirm the
    # linked card's normalized MPN matches the imported part.
    card = db_session.get(MaterialCard, item.material_card_id)
    assert card is not None
    assert card.normalized_mpn == normalize_mpn_key("MAX232")
    assert item.part_number == "MAX232"


def test_import_unresolvable_mpn_leaves_material_card_null(db_session: Session):
    """A punctuation-only MPN normalizes to empty — card stays null, line still
    imports."""
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner3@test.com", role="sales")
    el = create_excess_list(db_session, title="L", company_id=company.id, owner_id=owner.id)

    result = import_line_items(db_session, el.id, [{"part_number": "--", "quantity": "10"}])

    assert result["imported"] == 1
    item = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()
    assert item.material_card_id is None


# ---------------------------------------------------------------------------
# submit_offer — take_all
# ---------------------------------------------------------------------------


def test_take_all_offer_binds_list_no_lines(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o@test.com", role="sales")
    offerer = _make_user(db_session, email="b@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "MAX232"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="take_all",
        take_all_total_price=Decimal("9999.00"),
    )

    assert offer.id is not None
    assert offer.scope == "take_all"
    assert offer.status == "open"
    assert offer.take_all_total_price == Decimal("9999.00")
    assert db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).count() == 0


# ---------------------------------------------------------------------------
# submit_offer — per_line matching (part number only)
# ---------------------------------------------------------------------------


def test_per_line_matches_known_mpn(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o2@test.com", role="sales")
    offerer = _make_user(db_session, email="b2@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    target = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).one()

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "lm-358-n", "quantity": 50, "unit_price": Decimal("1.25")}],
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "matched"
    assert line.excess_line_item_id == target.id
    assert line.unit_price == Decimal("1.25")
    assert line.mpn_raw == "lm-358-n"


def test_per_line_queues_unknown_mpn_as_unmatched(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o3@test.com", role="sales")
    offerer = _make_user(db_session, email="b3@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "NOTONTHELIST99", "quantity": 5}],
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "unmatched"
    assert line.excess_line_item_id is None
    # QUEUED, never dropped — raw MPN preserved for manual resolution.
    assert line.mpn_raw == "NOTONTHELIST99"


def test_per_line_flags_duplicate_posting_mpn_as_ambiguous(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o4@test.com", role="sales")
    offerer = _make_user(db_session, email="b4@test.com", role="buyer")
    # Two posted lines share the same normalized part number → ambiguous.
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "LM-358-N"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "ambiguous"
    assert line.excess_line_item_id is None


def test_per_line_nullable_unit_price_allowed(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="o5@test.com", role="sales")
    offerer = _make_user(db_session, email="b5@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    offer = submit_offer(
        db_session,
        list_id=el.id,
        user=offerer,
        scope="per_line",
        lines=[{"mpn_raw": "LM358N", "quantity": 10}],  # no unit_price
    )

    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.unit_price is None
    assert line.match_status == "matched"


# ---------------------------------------------------------------------------
# submit_offer — guards
# ---------------------------------------------------------------------------


def test_self_offer_blocked(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner-self@test.com", role="trader")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=el.id, user=owner, scope="take_all")
    assert exc.value.status_code == 403
    assert "own" in exc.value.detail.lower()


def test_non_can_offer_user_blocked(db_session: Session):
    company = _make_company(db_session)
    owner = _make_user(db_session, email="owner-x@test.com", role="sales")
    sales_offerer = _make_user(db_session, email="sales-offerer@test.com", role="sales")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=el.id, user=sales_offerer, scope="take_all")
    assert exc.value.status_code == 403


def test_submit_offer_missing_list_404(db_session: Session):
    offerer = _make_user(db_session, email="b-404@test.com", role="buyer")
    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=999999, user=offerer, scope="take_all")
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# COLLECTING status wiring (Chunk D)
# ---------------------------------------------------------------------------


def test_open_list_flips_to_collecting_on_first_offer(db_session: Session):
    """An OPEN list flips to COLLECTING when the first offer lands (take_all)."""
    from app.constants import ExcessListStatus

    company = _make_company(db_session, name="SellerCo-Collecting")
    owner = _make_user(db_session, email="owner-coll@test.com", role="sales")
    offerer = _make_user(db_session, email="buyer-coll@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    # Manually flip to OPEN to simulate a published list (DRAFT → OPEN via publish endpoint).
    el.status = ExcessListStatus.OPEN
    db_session.commit()
    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN

    submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    db_session.refresh(el)
    assert el.status == ExcessListStatus.COLLECTING


def test_collecting_list_stays_collecting_on_subsequent_offer(db_session: Session):
    """A list already in COLLECTING stays in COLLECTING (idempotent flip)."""
    from app.constants import ExcessListStatus

    company = _make_company(db_session, name="SellerCo-Coll2")
    owner = _make_user(db_session, email="owner-coll2@test.com", role="sales")
    offerer1 = _make_user(db_session, email="buyer-coll2a@test.com", role="buyer")
    offerer2 = _make_user(db_session, email="buyer-coll2b@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["MAX232"])

    el.status = ExcessListStatus.OPEN
    db_session.commit()

    submit_offer(db_session, list_id=el.id, user=offerer1, scope="take_all")
    db_session.refresh(el)
    assert el.status == ExcessListStatus.COLLECTING

    # Second offer — status stays COLLECTING (not re-flipped to something else).
    submit_offer(db_session, list_id=el.id, user=offerer2, scope="take_all")
    db_session.refresh(el)
    assert el.status == ExcessListStatus.COLLECTING


# ---------------------------------------------------------------------------
# UI-submit offer attribution (finding #17, UI half) — buyer_company_id
# ---------------------------------------------------------------------------


def test_submit_offer_with_buyer_company_attributes_card(db_session: Session):
    """A UI-submit offer with buyer_company_id resolves offerer_vendor_card_id via
    counterparty_card — the attribution that lets the win-hook score a manual offer."""
    from app.services.resell_outreach_service import counterparty_card

    company = _make_company(db_session, name="Attr Seller")
    owner = _make_user(db_session, email="attr-owner@test.com", role="sales")
    offerer = _make_user(db_session, email="attr-buyer@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = ExcessListStatus.OPEN
    buyer_company = _make_company(db_session, name="Attr Buyer Co")
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all", buyer_company_id=buyer_company.id)

    assert offer.offerer_vendor_card_id is not None
    expected = counterparty_card(db_session, company_id=buyer_company.id)
    assert offer.offerer_vendor_card_id == expected.id


def test_submit_offer_without_buyer_company_leaves_card_none(db_session: Session):
    """No buyer attribution → offerer_vendor_card_id stays None (no regression — award
    still works, just no score, exactly as before)."""
    company = _make_company(db_session, name="NoAttr Seller")
    owner = _make_user(db_session, email="noattr-owner@test.com", role="sales")
    offerer = _make_user(db_session, email="noattr-buyer@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = ExcessListStatus.OPEN
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.offerer_vendor_card_id is None


def test_offer_form_renders_buyer_select(client, db_session: Session):
    """The submit-offer form renders the optional buyer <select> with company options
    (mirrors the create-modal company select; headless assert)."""
    from app.dependencies import require_user
    from app.main import app

    company = _make_company(db_session, name="Form Seller")
    owner = _make_user(db_session, email="form-owner@test.com", role="sales")
    viewer = _make_user(db_session, email="form-viewer@test.com", role="trader")  # non-owner offerer
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = ExcessListStatus.OPEN
    _make_company(db_session, name="Form Buyer Co")
    db_session.commit()

    app.dependency_overrides[require_user] = lambda: viewer
    try:
        resp = client.get(f"/v2/partials/resell/{el.id}/offer-form")
    finally:
        app.dependency_overrides.pop(require_user, None)

    assert resp.status_code == 200
    assert 'name="buyer_company_id"' in resp.text
    assert "Form Buyer Co" in resp.text


# ---------------------------------------------------------------------------
# Late-offer flagging (M3): an offer landing after the posting window closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("closed_status", [ExcessListStatus.BID_OUT, ExcessListStatus.AWARDED])
def test_offer_on_closed_list_is_flagged_late(db_session: Session, closed_status):
    """An inbound offer on a bid_out/awarded list is accepted but flagged ``late``
    (queued for review, never dropped) instead of a plain on-time ``open``."""
    company = _make_company(db_session, name=f"SellerCo-Late-{closed_status}")
    owner = _make_user(db_session, email=f"owner-late-{closed_status}@test.com", role="sales")
    offerer = _make_user(db_session, email=f"buyer-late-{closed_status}@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = closed_status
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.status == ExcessOfferStatus.LATE


@pytest.mark.parametrize("open_status", [ExcessListStatus.OPEN, ExcessListStatus.COLLECTING])
def test_offer_on_open_list_is_on_time(db_session: Session, open_status):
    """An offer while the window is still open (open/collecting) lands ``open``."""
    company = _make_company(db_session, name=f"SellerCo-OnTime-{open_status}")
    owner = _make_user(db_session, email=f"owner-ontime-{open_status}@test.com", role="sales")
    offerer = _make_user(db_session, email=f"buyer-ontime-{open_status}@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = open_status
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.status == ExcessOfferStatus.OPEN


# ---------------------------------------------------------------------------
# T3 — N+1 / joinedload regression guards (resell router context builders)
# ---------------------------------------------------------------------------


def _count_queries(db: Session, fn) -> int:
    """Count SQL statements executed on the session's engine while ``fn`` runs."""
    bind = db.get_bind()
    seen = {"n": 0}

    def _on(*_a, **_k):
        seen["n"] += 1

    event.listen(bind, "after_cursor_execute", _on)
    try:
        fn()
    finally:
        event.remove(bind, "after_cursor_execute", _on)
    return seen["n"]


def _seed_per_line_offers(db, el, submitter, line, *, n, tag):
    """Seed ``n`` per-line offers, each with a DISTINCT offerer company + card (so a
    per-offer lazy load is a real query, not an identity-map hit)."""
    for i in range(n):
        oc = Company(name=f"Offerer-{tag}-{i}")
        db.add(oc)
        db.flush()
        vc = VendorCard(normalized_name=f"offerer-{tag}-{i}", display_name=f"Offerer {tag} {i}")
        db.add(vc)
        db.flush()
        offer = ExcessOffer(
            excess_list_id=el.id,
            submitted_by=submitter.id,
            offerer_company_id=oc.id,
            offerer_vendor_card_id=vc.id,
            scope=ExcessOfferScope.PER_LINE,
            status=ExcessOfferStatus.OPEN,
        )
        db.add(offer)
        db.flush()
        db.add(
            ExcessOfferLine(
                offer_id=offer.id,
                excess_line_item_id=line.id,
                mpn_raw=line.part_number,
                quantity=10,
                unit_price=Decimal("0.50"),
                match_status=OfferLineMatchStatus.MATCHED,
            )
        )
    db.commit()


def test_offers_context_no_n_plus_1_across_offers(db_session: Session):
    """Owner Offers render: the SELECT count is INDEPENDENT of the offer count — the
    export-twin joinedloads (offerer_company / offerer_vendor_card / lines→excess_line_item)
    kill the per-offer/-line lazy loads (finding 1)."""
    owner = _make_user(db_session, email="offers-perf-owner@t.com")
    company = _make_company(db_session, name="OffersPerf Seller")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    line = db_session.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
    broker = _make_user(db_session, email="offers-perf-broker@t.com", role="buyer")

    def _build_and_walk():
        ctx = _offers_context(None, db_session, el, owner)
        # Touch every relationship the _offers.html template reads, forcing any lazy load.
        for offer in ctx["take_all_offers"]:
            _ = (offer.offerer_company, offer.offerer_vendor_card)
        for entries in ctx["by_line"].values():
            for e in entries:
                _ = (e["offer"].offerer_company, e["offer"].offerer_vendor_card, e["line"].excess_line_item)
        for e in ctx["unmatched"]:
            _ = (e["offer"].offerer_company, e["offer"].offerer_vendor_card, e["line"].excess_line_item)

    _seed_per_line_offers(db_session, el, broker, line, n=1, tag="a")
    db_session.expire_all()
    one = _count_queries(db_session, _build_and_walk)

    _seed_per_line_offers(db_session, el, broker, line, n=3, tag="b")
    db_session.expire_all()
    four = _count_queries(db_session, _build_and_walk)

    assert four == one, f"offers-tab N+1: 1 offer={one} queries, 4 offers={four}"


def test_award_response_context_loads_line_items_once(db_session: Session):
    """The award/unaward response merges _detail_context + _offers_context — each used
    to run the identical ExcessLineItem SELECT.

    Threading the preloaded items makes the standalone line-items SELECT run ONCE for
    the render.
    """
    owner = _make_user(db_session, email="award-perf-owner@t.com")
    company = _make_company(db_session, name="AwardPerf Seller")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N", "NE555P"])
    db_session.expire_all()

    selects: list[str] = []

    def _on(conn, cursor, statement, params, context, executemany):
        if "from excess_line_items" in statement.lower():
            selects.append(statement)

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", _on)
    try:
        _award_response_context(None, db_session, el, owner)
    finally:
        event.remove(bind, "before_cursor_execute", _on)

    assert len(selects) == 1, f"line-items loaded {len(selects)}× in award-response\n" + "\n".join(selects)


def test_tracker_context_no_n_plus_1_across_rows(db_session: Session):
    """Owner Outreach tracker render (runs inside the 3s poll): the SELECT count is
    INDEPENDENT of the row count — the export-twin joinedloads (target_vendor_card /
    excess_line_item / submitted_by_user) kill the per-row lazy loads (finding 2)."""
    owner = _make_user(db_session, email="tracker-perf-owner@t.com")
    company = _make_company(db_session, name="TrackerPerf Seller")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])

    def _add_rows(n, tag):
        # DISTINCT card + line + submitter per row so a per-row lazy load is a real query,
        # not an identity-map hit (a shared object would false-green the N+1).
        for i in range(n):
            vc = VendorCard(normalized_name=f"tk-{tag}-{i}", display_name=f"TK {tag} {i}")
            li = ExcessLineItem(excess_list_id=el.id, part_number=f"PN-{tag}-{i}", quantity=10)
            u = User(email=f"tk-sub-{tag}-{i}@t.com", name=f"Sub {tag}{i}", role="trader", azure_id=f"az-tk-{tag}-{i}")
            db_session.add_all([vc, li, u])
            db_session.flush()
            db_session.add(
                ExcessOutreach(
                    excess_list_id=el.id,
                    excess_line_item_id=li.id,
                    target_vendor_card_id=vc.id,
                    submitted_by=u.id,
                    channel=ExcessOutreachChannel.EMAIL,
                    status=ExcessOutreachStatus.SENT,
                )
            )
        db_session.commit()

    def _build_and_walk():
        ctx = _outreach_tracker_context(None, db_session, el, owner)
        for r in ctx["rows"]:
            _ = (
                r.target_vendor_card.display_name if r.target_vendor_card else None,
                r.excess_line_item.part_number if r.excess_line_item else None,
                r.submitted_by_user.name if r.submitted_by_user else None,
            )

    _add_rows(1, "a")
    db_session.expire_all()
    one = _count_queries(db_session, _build_and_walk)

    _add_rows(3, "b")
    db_session.expire_all()
    four = _count_queries(db_session, _build_and_walk)

    assert four == one, f"tracker N+1: 1 row={one} queries, 4 rows={four}"


# ---------------------------------------------------------------------------
# Finding #47 — submit_offer's own posted-status guard (service-level, not just router)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blocked_status", [ExcessListStatus.DRAFT, ExcessListStatus.CLOSED, ExcessListStatus.EXPIRED])
def test_submit_offer_rejects_non_posted_list_service_level(db_session: Session, blocked_status):
    """A direct service call (bypassing the router's own 404-camouflage guard) must
    itself reject a draft/terminal list — mirrors ``upload_bids``'s own guard."""
    company = _make_company(db_session, name=f"NonPosted-{blocked_status}")
    owner = _make_user(db_session, email=f"np-owner-{blocked_status}@test.com", role="sales")
    offerer = _make_user(db_session, email=f"np-buyer-{blocked_status}@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = blocked_status
    db_session.commit()

    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")
    assert exc.value.status_code == 409
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).count() == 0


@pytest.mark.parametrize(
    ("posted_status", "expected_offer_status"),
    [
        (ExcessListStatus.OPEN, ExcessOfferStatus.OPEN),
        (ExcessListStatus.COLLECTING, ExcessOfferStatus.OPEN),
        # Resolved-but-awardable lists still ACCEPT the offer, but stamp it late
        # (offer_status_for_list: the posting already reads as closed).
        (ExcessListStatus.BID_OUT, ExcessOfferStatus.LATE),
        (ExcessListStatus.AWARDED, ExcessOfferStatus.LATE),
    ],
)
def test_submit_offer_works_on_every_posted_status(db_session: Session, posted_status, expected_offer_status):
    """Control: every posted status (live or resolved-but-awardable) still accepts an
    offer through the service directly — persisted with the honest open/late stamp."""
    company = _make_company(db_session, name=f"Posted-{posted_status}")
    owner = _make_user(db_session, email=f"posted-owner-{posted_status}@test.com", role="sales")
    offerer = _make_user(db_session, email=f"posted-buyer-{posted_status}@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.status = posted_status
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    persisted = db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).one()
    assert persisted.id == offer.id
    assert persisted.submitted_by == offerer.id
    assert persisted.scope == ExcessOfferScope.TAKE_ALL
    assert persisted.status == expected_offer_status


# ---------------------------------------------------------------------------
# Finding #9 — submit_offer takes the M9 lock + re-checks post-lock status
# ---------------------------------------------------------------------------


def test_submit_offer_locks_list_before_status_read(db_session: Session, monkeypatch):
    """``submit_offer`` takes the M9 list-row lock (``_lock_list_row``) BEFORE re-
    reading ``excess_list.status`` — spy the hook to prove it's wired
    (``with_for_update`` is a no-op on the SQLite test engine, so the actual race is
    unobservable here)."""
    from app.services import excess_service

    company = _make_company(db_session, name="Lock Seller")
    owner = _make_user(db_session, email="lock-owner@test.com", role="sales")
    offerer = _make_user(db_session, email="lock-buyer@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    db_session.commit()

    calls: list[int] = []
    real_lock = excess_service._lock_list_row

    def _spy(db, excess_list_id):
        calls.append(excess_list_id)
        return real_lock(db, excess_list_id)

    monkeypatch.setattr(excess_service, "_lock_list_row", _spy)

    submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert calls == [el.id]


def test_submit_offer_stale_read_cannot_resurrect_closed_list(db_session: Session):
    """Finding #9: a list closed BETWEEN the caller's stale read and the lock must not
    be resurrected by the open->collecting flip.

    Simulates the race within one SQLite session: a raw core UPDATE (bypassing the ORM,
    like a second transaction's committed close) flips the list CLOSED behind the
    already-identity-mapped object's back, with no intervening commit (so
    ``expire_on_commit`` never auto-refreshes it). Without the M9 lock's
    ``populate_existing`` refresh, ``submit_offer`` would read the stale 'open' status and
    proceed to create the offer / flip to collecting on a dead list.
    """
    from sqlalchemy import text as sa_text

    company = _make_company(db_session, name="StaleRace Seller")
    owner = _make_user(db_session, email="stale-owner@test.com", role="sales")
    offerer = _make_user(db_session, email="stale-buyer@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    db_session.commit()
    assert el.status == ExcessListStatus.OPEN

    db_session.execute(sa_text("UPDATE excess_lists SET status = 'closed' WHERE id = :id").bindparams(id=el.id))
    assert el.status == ExcessListStatus.OPEN  # still stale, pre-call

    with pytest.raises(HTTPException) as exc:
        submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")
    assert exc.value.status_code == 409
    assert el.status == ExcessListStatus.CLOSED  # the lock refreshed it in place
    assert db_session.query(ExcessOffer).filter_by(excess_list_id=el.id).count() == 0


# ---------------------------------------------------------------------------
# Finding #10 — an offer landing after close_at (but before the status is swept) is LATE
# ---------------------------------------------------------------------------


def test_submit_offer_past_close_at_is_late_even_though_status_still_open(db_session: Session):
    """A D1 posting window that lapsed at ``close_at`` but hasn't been swept to
    bid_out/expired yet (the nightly job hasn't run) still stamps a landing offer
    ``late`` — not an indistinguishable on-time ``open``."""
    company = _make_company(db_session, name="LapsedWindow Seller")
    owner = _make_user(db_session, email="lapsed-owner@test.com", role="sales")
    offerer = _make_user(db_session, email="lapsed-buyer@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.close_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.commit()
    assert el.status == ExcessListStatus.OPEN  # the nightly sweep hasn't run

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.status == ExcessOfferStatus.LATE


def test_submit_offer_before_close_at_is_open(db_session: Session):
    """Control: a future close_at (window still live) still stamps ``open``."""
    company = _make_company(db_session, name="FutureWindow Seller")
    owner = _make_user(db_session, email="future-owner@test.com", role="sales")
    offerer = _make_user(db_session, email="future-buyer@test.com", role="buyer")
    el = _make_list_with_lines(db_session, owner, company, ["LM358N"])
    el.close_at = datetime.now(UTC) + timedelta(hours=2)
    db_session.commit()

    offer = submit_offer(db_session, list_id=el.id, user=offerer, scope="take_all")

    assert offer.status == ExcessOfferStatus.OPEN
