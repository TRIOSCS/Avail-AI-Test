"""test_resell_bid_back.py — Bid-back assembly + clean export + posting window (Chunk
E).

Covers the outbound bid-back the owner assembles from collected inbound offers:
  • ``build_bid_back`` seeds ``customer_unit_price`` from each line's ``best_offer_unit_price``
    rollup, and respects a per-line override;
  • ``build_bid_back`` is owner-only (non-owner → 403);
  • ``bid_back_export_context`` is CLEAN — its line dicts contain ONLY part/mfr/qty/
    condition/unit price and carry NO trader / vendor / offerer / source keys (the
    cleanliness is enforced at assembly, asserted on the dict keys explicitly);
  • the PDF generator returns bytes for a seeded bid;
  • ``publish_list`` stamps ``open_at``; ``close_list`` stamps ``close_at`` owner-only.

WeasyPrint requires system libs; the PDF test runs against the real renderer when
available and otherwise falls back to a fake (mirrors tests/test_document_service.py).

Called by: pytest
Depends on: app.services.bid_back_service, app.services.excess_service,
    app.services.excess_mirror, app.models.excess, tests.conftest
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import CustomerBidStatus, ExcessListStatus
from app.models import Company, User
from app.models.excess import CustomerBid, ExcessLineItem, ExcessList, ExcessOffer
from app.services import bid_back_service, excess_service
from app.utils.normalization import normalize_mpn_key

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="Globex Stock Holdings")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owner(db_session: Session) -> User:
    user = User(
        email="bidback-owner@trioscs.com",
        name="Olivia Owner",
        role="trader",
        azure_id="bidback-owner-001",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session: Session) -> User:
    user = User(
        email="bidback-other@trioscs.com",
        name="Ned NonOwner",
        role="trader",
        azure_id="bidback-other-001",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def priced_list(db_session: Session, owner: User, seller_company: Company) -> ExcessList:
    """A collecting list with two lines that already have a best-offer rollup price."""
    el = ExcessList(
        title="Globex excess Q3",
        company_id=seller_company.id,
        owner_id=owner.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    for mpn, qty, best in (("XCVU9P-2FLGA2104I", 100, Decimal("142.5000")), ("EP4CE10F17C8N", 250, Decimal("8.7500"))):
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=mpn,
                normalized_part_number=normalize_mpn_key(mpn),
                manufacturer="AMD/Xilinx",
                quantity=qty,
                condition="New",
                best_offer_unit_price=best,
                offer_count=1,
            )
        )
    db_session.commit()
    db_session.refresh(el)
    return el


def _lines(db: Session, el: ExcessList) -> list[ExcessLineItem]:
    return db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()


# ── Assembly: seeding from best_offer_unit_price ─────────────────────


def test_build_bid_back_seeds_from_best_offer_price(db_session, owner, priced_list):
    """Each selected line's customer_unit_price defaults to its
    best_offer_unit_price."""
    items = _lines(db_session, priced_list)
    selections = [{"excess_line_item_id": it.id} for it in items]

    bid = bid_back_service.build_bid_back(db_session, list_id=priced_list.id, owner=owner, selections=selections)

    assert isinstance(bid, CustomerBid)
    assert bid.status == CustomerBidStatus.DRAFT
    assert bid.owner_id == owner.id
    by_line = {ln.excess_line_item_id: ln for ln in bid.lines}
    assert len(by_line) == 2
    for it in items:
        ln = by_line[it.id]
        assert ln.customer_unit_price == it.best_offer_unit_price
        assert ln.quantity == it.quantity


def test_build_bid_back_respects_override(db_session, owner, priced_list):
    """A per-line override price wins over the best-offer seed."""
    items = _lines(db_session, priced_list)
    override = Decimal("130.0000")
    selections = [
        {"excess_line_item_id": items[0].id, "customer_unit_price": override},
        {"excess_line_item_id": items[1].id},  # seeded
    ]

    bid = bid_back_service.build_bid_back(db_session, list_id=priced_list.id, owner=owner, selections=selections)

    by_line = {ln.excess_line_item_id: ln for ln in bid.lines}
    assert by_line[items[0].id].customer_unit_price == override
    assert by_line[items[1].id].customer_unit_price == items[1].best_offer_unit_price


def test_build_bid_back_non_owner_forbidden(db_session, other_user, priced_list):
    """Only the list owner may assemble a bid-back (403)."""
    items = _lines(db_session, priced_list)
    with pytest.raises(HTTPException) as exc:
        bid_back_service.build_bid_back(
            db_session,
            list_id=priced_list.id,
            owner=other_user,
            selections=[{"excess_line_item_id": items[0].id}],
        )
    assert exc.value.status_code == 403


def test_build_bid_back_rejects_foreign_line(db_session, owner, priced_list, seller_company):
    """A selection naming a line from another list is rejected (404), never priced."""
    other = ExcessList(title="other", company_id=seller_company.id, owner_id=owner.id, status="draft")
    db_session.add(other)
    db_session.flush()
    foreign = ExcessLineItem(excess_list_id=other.id, part_number="X", quantity=1)
    db_session.add(foreign)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        bid_back_service.build_bid_back(
            db_session,
            list_id=priced_list.id,
            owner=owner,
            selections=[{"excess_line_item_id": foreign.id}],
        )
    assert exc.value.status_code == 404


# ── Corrupt best_offer_id must never 500 (P1 #1 hotfix) ──────────────


def test_build_bid_back_nulls_unresolvable_best_offer(db_session, owner, priced_list):
    """A line whose ``best_offer_id`` points to a non-existent ExcessOffer must NOT 500.

    Mirrors the corrupt staging state (``best_offer_id`` held an offer-LINE id, not an
    ExcessOffer id, so it resolved to no ``excess_offers`` row). ``selected_offer_id`` is a
    real FK, so seeding it with that dangling value used to raise IntegrityError on commit
    → an unhandled 500. Assembly must instead NULL the unresolvable pointer and still build
    the bid — and a manual price override on another line must keep working.
    """
    items = _lines(db_session, priced_list)
    # Point the first line at an ExcessOffer id that does not exist (the corruption).
    dangling_offer_id = 987654
    assert db_session.get(ExcessOffer, dangling_offer_id) is None
    items[0].best_offer_id = dangling_offer_id
    db_session.commit()

    override = Decimal("99.0000")
    selections = [
        {"excess_line_item_id": items[0].id},  # corrupt pointer → must null, not 500
        {"excess_line_item_id": items[1].id, "customer_unit_price": override},  # manual override
    ]

    # Must not raise IntegrityError / 500.
    bid = bid_back_service.build_bid_back(db_session, list_id=priced_list.id, owner=owner, selections=selections)

    by_line = {ln.excess_line_item_id: ln for ln in bid.lines}
    # The dangling pointer was dropped — but the line is still priced + assembled.
    assert by_line[items[0].id].selected_offer_id is None
    assert by_line[items[0].id].customer_unit_price == items[0].best_offer_unit_price
    # The manual override still applies on the other line.
    assert by_line[items[1].id].customer_unit_price == override


def test_build_bid_back_records_valid_best_offer(db_session, owner, priced_list):
    """When ``best_offer_id`` resolves to a real ExcessOffer, it is recorded as
    provenance."""
    items = _lines(db_session, priced_list)
    offer = ExcessOffer(excess_list_id=priced_list.id, submitted_by=owner.id, scope="per_line", status="open")
    db_session.add(offer)
    db_session.flush()
    items[0].best_offer_id = offer.id
    db_session.commit()

    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=priced_list.id,
        owner=owner,
        selections=[{"excess_line_item_id": items[0].id}],
    )
    line = next(ln for ln in bid.lines if ln.excess_line_item_id == items[0].id)
    assert line.selected_offer_id == offer.id


# ── Export context cleanliness (the load-bearing assertion) ──────────

# The exact, whitelisted set of keys the customer doc may carry per line. The doc shows
# part / mfr / qty / condition / our unit + extended price — and NOTHING that could leak
# a broker / trader / source.
_ALLOWED_LINE_KEYS = {"part_number", "manufacturer", "quantity", "condition", "unit_price", "extended_price"}

# Any key whose presence would leak who offered / sourced the part — must never appear.
_FORBIDDEN_LINE_SUBSTRINGS = ("vendor", "offer", "trader", "source", "broker", "best_offer", "submitted")


def test_export_context_line_keys_are_whitelisted(db_session, owner, priced_list):
    """Every export line dict carries ONLY the clean whitelist of keys — no more."""
    items = _lines(db_session, priced_list)
    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=priced_list.id,
        owner=owner,
        selections=[{"excess_line_item_id": it.id} for it in items],
    )
    ctx = bid_back_service.bid_back_export_context(bid)

    assert ctx["line_items"], "export must carry the priced lines"
    for line in ctx["line_items"]:
        assert set(line.keys()) == _ALLOWED_LINE_KEYS, f"unexpected keys: {set(line.keys()) - _ALLOWED_LINE_KEYS}"


def test_export_context_strips_trader_and_source_fields(db_session, owner, priced_list):
    """No line key contains a trader/vendor/offerer/source token (stripping proof)."""
    items = _lines(db_session, priced_list)
    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=priced_list.id,
        owner=owner,
        selections=[{"excess_line_item_id": it.id} for it in items],
    )
    ctx = bid_back_service.bid_back_export_context(bid)

    for line in ctx["line_items"]:
        for key in line:
            low = key.lower()
            assert not any(tok in low for tok in _FORBIDDEN_LINE_SUBSTRINGS), f"leaky key: {key}"

    # The header carries no seller-company identity either (anonymized customer doc).
    header_blob = " ".join(str(v) for v in ctx.values() if isinstance(v, (str, int, float)))
    assert "Globex" not in header_blob


def test_export_context_prices_and_totals(db_session, owner, priced_list):
    """Export reflects seeded prices + computes extended + subtotal."""
    items = _lines(db_session, priced_list)
    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=priced_list.id,
        owner=owner,
        selections=[{"excess_line_item_id": it.id} for it in items],
    )
    ctx = bid_back_service.bid_back_export_context(bid)

    expected_subtotal = sum(float(it.best_offer_unit_price) * it.quantity for it in items)
    assert ctx["subtotal"] == pytest.approx(expected_subtotal)
    first = next(li for li in ctx["line_items"] if li["part_number"] == items[0].part_number)
    assert first["extended_price"] == pytest.approx(float(items[0].best_offer_unit_price) * items[0].quantity)


# ── PDF generation ───────────────────────────────────────────────────


def test_generate_bid_report_pdf_returns_bytes(db_session, owner, priced_list, monkeypatch):
    """The bid PDF generator returns non-empty PDF bytes for a seeded bid.

    WeasyPrint's HTML(...).write_pdf() is stubbed so the result is deterministic
    regardless of whether the real renderer (or another test's global fake) is loaded —
    the assertion under test is that the generator wires the clean context through the
    template to write_pdf and returns its bytes.
    """
    items = _lines(db_session, priced_list)
    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=priced_list.id,
        owner=owner,
        selections=[{"excess_line_item_id": it.id} for it in items],
    )

    class _FakeHTML:
        def __init__(self, *, string):
            self._string = string

        def write_pdf(self):
            return b"%PDF-1.4 stub"

    import weasyprint

    monkeypatch.setattr(weasyprint, "HTML", _FakeHTML)

    from app.services.document_service import generate_bid_report_pdf

    pdf = generate_bid_report_pdf(bid.id, db_session)
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:4] == b"%PDF"


def test_generate_bid_report_pdf_missing_bid(db_session):
    """A missing bid id raises ValueError (the documents router maps it to 404)."""
    from app.services.document_service import generate_bid_report_pdf

    with pytest.raises(ValueError, match="not found"):
        generate_bid_report_pdf(999999, db_session)


def test_bid_pdf_html_omits_seller_and_broker(db_session, owner, priced_list, monkeypatch):
    """The rendered HTML the PDF is built from contains no seller/broker identity.

    Intercepts the WeasyPrint HTML(string=...) call to assert the rendered markup is
    clean — defense in depth on top of the context-key assertions.
    """
    items = _lines(db_session, priced_list)
    bid = bid_back_service.build_bid_back(
        db_session,
        list_id=priced_list.id,
        owner=owner,
        selections=[{"excess_line_item_id": it.id} for it in items],
    )

    captured = {}

    class _FakeHTML:
        def __init__(self, *, string):
            captured["html"] = string

        def write_pdf(self):
            return b"%PDF-1.4 fake"

    import weasyprint

    monkeypatch.setattr(weasyprint, "HTML", _FakeHTML)

    from app.services.document_service import generate_bid_report_pdf

    generate_bid_report_pdf(bid.id, db_session)

    html = captured["html"]
    assert "Globex" not in html  # seller company name absent
    assert "Customer Excess" not in html  # internal vendor label absent
    assert items[0].part_number in html  # the parts ARE present


# ── Posting window: open_at / close_at ───────────────────────────────


def test_publish_sets_open_at(db_session, owner, seller_company):
    """publish_list stamps open_at now that the column exists."""
    from app.services import excess_mirror

    el = ExcessList(
        title="to publish",
        company_id=seller_company.id,
        owner_id=owner.id,
        status=ExcessListStatus.DRAFT,
    )
    db_session.add(el)
    db_session.flush()
    db_session.add(ExcessLineItem(excess_list_id=el.id, part_number="LM358N", quantity=10))
    db_session.commit()

    assert el.open_at is None
    excess_mirror.publish_list(db_session, el.id, owner)
    db_session.refresh(el)
    assert el.status == ExcessListStatus.OPEN
    assert el.open_at is not None


def test_close_list_sets_close_at_owner_only(db_session, owner, other_user, priced_list):
    """close_list stamps close_at + flips status; non-owner is forbidden (403)."""
    # Non-owner blocked first — no mutation.
    with pytest.raises(HTTPException) as exc:
        excess_service.close_list(db_session, priced_list.id, other_user)
    assert exc.value.status_code == 403
    db_session.refresh(priced_list)
    assert priced_list.close_at is None

    closed = excess_service.close_list(db_session, priced_list.id, owner)
    assert closed.close_at is not None
    assert closed.status == ExcessListStatus.BID_OUT
