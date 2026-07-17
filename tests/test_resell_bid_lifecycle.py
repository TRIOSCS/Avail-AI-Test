"""test_resell_bid_lifecycle.py — CustomerBid send/accept/reject + revisioning (M4).

Covers the bid-back lifecycle the M4 rework adds on top of the shipped assembly:
  • re-assembling a list's bid BUMPS ``revision`` on the SAME CustomerBid row (audit
    chain preserved) instead of orphaning a fresh draft, and replaces its lines;
  • ``resolve_seller_contact`` resolves the seller's send email from the list's site,
    a company-level site fallback, or a primary SiteContact — and (None, None) when none;
  • ``send_bid_back`` emails the clean PDF (mocked) and flips ``draft→sent`` stamping
    ``sent_at``, only on a confirmed send (409 non-draft / no lines, 422 no email, 502 on
    a failed send);
  • ``record_bid_response`` records the seller's answer ``sent→accepted/rejected`` with
    who/when (409 unless ``sent``);
  • the send / accept / reject ROUTES are owner-gated and re-render the Build-Bid tab.

The email send is mocked at the source (``email_service.send_batch_rfq``) and the PDF
render is stubbed (``document_service.generate_bid_report_pdf``) so no Graph/WeasyPrint
dependency is needed.

Called by: pytest
Depends on: app.services.bid_back_service, app.models.excess, app.models.crm, tests.conftest
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.constants import CustomerBidStatus, ExcessListStatus
from app.models import Company, User
from app.models.crm import CustomerSite, SiteContact
from app.models.excess import CustomerBid, CustomerBidLine, ExcessLineItem, ExcessList
from app.services import bid_back_service
from app.utils.normalization import normalize_mpn_key

# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def seller_company(db_session: Session) -> Company:
    co = Company(name="Initech Surplus")
    db_session.add(co)
    db_session.commit()
    db_session.refresh(co)
    return co


@pytest.fixture()
def owner(db_session: Session) -> User:
    user = User(email="bl-owner@trioscs.com", name="Bea Owner", role="trader", azure_id="bl-owner-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def other_user(db_session: Session) -> User:
    user = User(email="bl-other@trioscs.com", name="Ozzy Other", role="trader", azure_id="bl-other-1")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture()
def priced_list(db_session: Session, owner: User, seller_company: Company) -> ExcessList:
    el = ExcessList(
        title="Initech excess",
        company_id=seller_company.id,
        owner_id=owner.id,
        status=ExcessListStatus.COLLECTING,
        total_line_items=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    for mpn, qty, best in (("LM317T", 500, Decimal("0.5000")), ("NE555P", 800, Decimal("0.2500"))):
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=mpn,
                normalized_part_number=normalize_mpn_key(mpn),
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


def _assemble(db: Session, el: ExcessList, owner: User, line_ids=None) -> CustomerBid:
    items = _lines(db, el)
    sel = [{"excess_line_item_id": it.id} for it in items if line_ids is None or it.id in line_ids]
    return bid_back_service.build_bid_back(db, list_id=el.id, owner=owner, selections=sel)


def _seed_site_email(db: Session, company: Company, email: str = "buyer@initech.com") -> CustomerSite:
    site = CustomerSite(company_id=company.id, site_name="HQ", contact_name="Sam Seller", contact_email=email)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


# ── Re-assemble: revision bump on the SAME row ───────────────────────


def test_reassemble_bumps_revision_on_same_row(db_session, owner, priced_list):
    """Re-assembling a list's bid bumps revision on the SAME CustomerBid row — no
    orphan."""
    first = _assemble(db_session, priced_list, owner)
    assert first.revision == 1
    assert first.status == CustomerBidStatus.DRAFT

    second = _assemble(db_session, priced_list, owner)

    assert second.id == first.id  # same row, not a new orphan draft
    assert second.revision == 2
    # Exactly ONE CustomerBid row exists for the list (history is the revision counter).
    assert db_session.query(CustomerBid).filter_by(excess_list_id=priced_list.id).count() == 1


def test_reassemble_replaces_lines(db_session, owner, priced_list):
    """A re-assemble replaces the prior revision's lines (no stale duplicates)."""
    items = _lines(db_session, priced_list)
    _assemble(db_session, priced_list, owner)  # both lines
    bid = _assemble(db_session, priced_list, owner, line_ids={items[0].id})  # only the first

    db_session.refresh(bid)
    assert len(bid.lines) == 1
    assert bid.lines[0].excess_line_item_id == items[0].id
    # No orphaned CustomerBidLine rows linger from the superseded revision.
    assert db_session.query(CustomerBidLine).filter_by(customer_bid_id=bid.id).count() == 1


def test_reassemble_after_sent_resets_to_fresh_draft(db_session, owner, priced_list):
    """Re-assembling a SENT bid bumps revision and resets it to a fresh draft (stamps
    cleared)."""
    bid = _assemble(db_session, priced_list, owner)
    bid.status = CustomerBidStatus.SENT
    bid.sent_at = datetime.now(UTC)
    db_session.commit()

    again = _assemble(db_session, priced_list, owner)

    assert again.id == bid.id
    assert again.revision == 2
    assert again.status == CustomerBidStatus.DRAFT
    assert again.sent_at is None


# ── Re-assemble after a TERMINAL bid forks a NEW immutable row (D3) ───


def test_reassemble_after_accepted_creates_new_immutable_row(db_session, owner, priced_list):
    """D3: re-assembling off an ACCEPTED bid INSERTs a NEW draft revision; the accepted
    row is frozen history — its status and send/response stamps stay untouched, and it
    keeps its own lines (they are NOT deleted into the new revision)."""
    accepted = _assemble(db_session, priced_list, owner)
    accepted.status = CustomerBidStatus.ACCEPTED
    accepted.sent_at = datetime.now(UTC)
    accepted.responded_at = datetime.now(UTC)
    accepted.responded_by_id = owner.id
    db_session.commit()
    accepted_id = accepted.id

    fresh = _assemble(db_session, priced_list, owner)

    # A brand-new row, not a mutation of the accepted one.
    assert fresh.id != accepted_id
    assert fresh.status == CustomerBidStatus.DRAFT
    assert fresh.revision == 2
    assert len(fresh.lines) == 2  # the new revision carries its own freshly-built lines
    # Two rows now exist for the list: the frozen accepted revision + the new draft.
    assert db_session.query(CustomerBid).filter_by(excess_list_id=priced_list.id).count() == 2
    # The accepted row is UNTOUCHED — frozen history.
    frozen = db_session.get(CustomerBid, accepted_id)
    assert frozen.status == CustomerBidStatus.ACCEPTED
    assert frozen.revision == 1
    assert frozen.sent_at is not None
    assert frozen.responded_at is not None
    assert frozen.responded_by_id == owner.id
    assert len(frozen.lines) == 2  # the accepted revision keeps its own lines


def test_reassemble_after_rejected_creates_new_draft_row(db_session, owner, priced_list):
    """D3: rejected→revise forks a new draft revision too; the rejected row stays put."""
    rejected = _assemble(db_session, priced_list, owner)
    rejected.status = CustomerBidStatus.REJECTED
    rejected.responded_at = datetime.now(UTC)
    db_session.commit()
    rejected_id = rejected.id

    fresh = _assemble(db_session, priced_list, owner)

    assert fresh.id != rejected_id
    assert fresh.status == CustomerBidStatus.DRAFT
    assert fresh.revision == 2
    frozen = db_session.get(CustomerBid, rejected_id)
    assert frozen.status == CustomerBidStatus.REJECTED
    assert db_session.query(CustomerBid).filter_by(excess_list_id=priced_list.id).count() == 2


def test_reassemble_after_accepted_new_row_is_latest(db_session, owner, priced_list):
    """The forked draft is what the id-desc select (_latest_bid / Build-Bid tab)
    surfaces as the newest revision — the frozen row must not shadow it."""
    accepted = _assemble(db_session, priced_list, owner)
    accepted.status = CustomerBidStatus.ACCEPTED
    db_session.commit()

    fresh = _assemble(db_session, priced_list, owner)

    latest = (
        db_session.query(CustomerBid).filter_by(excess_list_id=priced_list.id).order_by(CustomerBid.id.desc()).first()
    )
    assert latest.id == fresh.id


# ── resolve_seller_contact ───────────────────────────────────────────


def test_resolve_seller_contact_from_list_site(db_session, owner, seller_company):
    """The list's own customer_site contact email wins."""
    site = _seed_site_email(db_session, seller_company, "site@initech.com")
    el = ExcessList(
        title="x", company_id=seller_company.id, owner_id=owner.id, customer_site_id=site.id, status="collecting"
    )
    db_session.add(el)
    db_session.commit()
    name, email = bid_back_service.resolve_seller_contact(db_session, el)
    assert email == "site@initech.com"
    assert name == "Sam Seller"


def test_resolve_seller_contact_company_site_fallback(db_session, owner, seller_company, priced_list):
    """With no list site set, an active company site's contact email is the fallback."""
    _seed_site_email(db_session, seller_company, "fallback@initech.com")
    name, email = bid_back_service.resolve_seller_contact(db_session, priced_list)
    assert email == "fallback@initech.com"


def test_resolve_seller_contact_primary_site_contact(db_session, owner, seller_company, priced_list):
    """A site with no site-level email falls through to its primary SiteContact."""
    site = CustomerSite(company_id=seller_company.id, site_name="Branch")
    db_session.add(site)
    db_session.flush()
    db_session.add(
        SiteContact(customer_site_id=site.id, full_name="Percy Primary", email="percy@initech.com", is_primary=True)
    )
    db_session.commit()
    name, email = bid_back_service.resolve_seller_contact(db_session, priced_list)
    assert email == "percy@initech.com"
    assert name == "Percy Primary"


def test_resolve_seller_contact_none_when_no_email(db_session, owner, priced_list):
    """No site + no contact anywhere → (None, None) so the caller refuses to send."""
    name, email = bid_back_service.resolve_seller_contact(db_session, priced_list)
    assert (name, email) == (None, None)


# ── send_bid_back (service, email mocked) ────────────────────────────


def _sent_ok(email: str):
    return [{"vendor_email": email, "status": "sent"}]


async def test_send_bid_back_flips_to_sent(db_session, owner, seller_company, priced_list):
    """A confirmed send flips draft→sent, stamps sent_at, and attaches the clean
    PDF."""
    _seed_site_email(db_session, seller_company, "buyer@initech.com")
    bid = _assemble(db_session, priced_list, owner)

    send_mock = AsyncMock(return_value=_sent_ok("buyer@initech.com"))
    with (
        patch("app.email_service.send_batch_rfq", new=send_mock),
        patch("app.services.document_service.generate_bid_report_pdf", return_value=b"%PDF stub"),
    ):
        result = await bid_back_service.send_bid_back(
            db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, token="tok"
        )

    assert result.status == CustomerBidStatus.SENT
    assert result.sent_at is not None
    # send_batch_rfq called with NO requisition + exactly one PDF attachment.
    kwargs = send_mock.await_args.kwargs
    assert kwargs["requisition_id"] is None
    assert len(kwargs["attachments"]) == 1
    assert kwargs["attachments"][0].content_type == "application/pdf"


async def test_send_bid_back_requires_draft(db_session, owner, seller_company, priced_list):
    """A non-draft bid cannot be re-sent (409); the status is untouched."""
    _seed_site_email(db_session, seller_company)
    bid = _assemble(db_session, priced_list, owner)
    bid.status = CustomerBidStatus.SENT
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await bid_back_service.send_bid_back(
            db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, token="tok"
        )
    assert exc.value.status_code == 409


async def test_send_bid_back_no_email_422(db_session, owner, priced_list):
    """No customer contact email on file → 422 (never email nobody), bid stays
    draft."""
    bid = _assemble(db_session, priced_list, owner)
    with pytest.raises(HTTPException) as exc:
        await bid_back_service.send_bid_back(
            db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, token="tok"
        )
    assert exc.value.status_code == 422
    db_session.refresh(bid)
    assert bid.status == CustomerBidStatus.DRAFT


async def test_send_bid_back_failed_send_502(db_session, owner, seller_company, priced_list):
    """A non-'sent' send result raises 502 and leaves the bid a draft (no false
    stamp)."""
    _seed_site_email(db_session, seller_company, "buyer@initech.com")
    bid = _assemble(db_session, priced_list, owner)
    with (
        patch(
            "app.email_service.send_batch_rfq",
            new=AsyncMock(return_value=[{"vendor_email": "buyer@initech.com", "status": "skipped"}]),
        ),
        patch("app.services.document_service.generate_bid_report_pdf", return_value=b"%PDF stub"),
    ):
        with pytest.raises(HTTPException) as exc:
            await bid_back_service.send_bid_back(
                db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, token="tok"
            )
    assert exc.value.status_code == 502
    db_session.refresh(bid)
    assert bid.status == CustomerBidStatus.DRAFT
    assert bid.sent_at is None


async def test_send_bid_back_non_owner_403(db_session, owner, other_user, seller_company, priced_list):
    """Only the list owner may send the bid (403)."""
    _seed_site_email(db_session, seller_company)
    bid = _assemble(db_session, priced_list, owner)
    with pytest.raises(HTTPException) as exc:
        await bid_back_service.send_bid_back(
            db_session, list_id=priced_list.id, bid_id=bid.id, owner=other_user, token="tok"
        )
    assert exc.value.status_code == 403


# ── record_bid_response (accept / reject) ────────────────────────────


def _sent_bid(db: Session, el: ExcessList, owner: User) -> CustomerBid:
    bid = _assemble(db, el, owner)
    bid.status = CustomerBidStatus.SENT
    bid.sent_at = datetime.now(UTC)
    db.commit()
    db.refresh(bid)
    return bid


def test_record_bid_response_accept(db_session, owner, priced_list):
    """Accepting a sent bid stamps who/when and flips sent→accepted."""
    bid = _sent_bid(db_session, priced_list, owner)
    result = bid_back_service.record_bid_response(
        db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, accepted=True
    )
    assert result.status == CustomerBidStatus.ACCEPTED
    assert result.responded_at is not None
    assert result.responded_by_id == owner.id


def test_record_bid_response_reject(db_session, owner, priced_list):
    """Rejecting a sent bid flips sent→rejected."""
    bid = _sent_bid(db_session, priced_list, owner)
    result = bid_back_service.record_bid_response(
        db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, accepted=False
    )
    assert result.status == CustomerBidStatus.REJECTED
    assert result.responded_by_id == owner.id


def test_record_bid_response_cannot_accept_draft(db_session, owner, priced_list):
    """A draft (never sent) bid cannot be accepted (409)."""
    bid = _assemble(db_session, priced_list, owner)
    with pytest.raises(HTTPException) as exc:
        bid_back_service.record_bid_response(
            db_session, list_id=priced_list.id, bid_id=bid.id, owner=owner, accepted=True
        )
    assert exc.value.status_code == 409
    db_session.refresh(bid)
    assert bid.status == CustomerBidStatus.DRAFT


def test_record_bid_response_non_owner_403(db_session, owner, other_user, priced_list):
    """Only the owner may record the seller's answer (403)."""
    bid = _sent_bid(db_session, priced_list, owner)
    with pytest.raises(HTTPException) as exc:
        bid_back_service.record_bid_response(
            db_session, list_id=priced_list.id, bid_id=bid.id, owner=other_user, accepted=True
        )
    assert exc.value.status_code == 403


# ── Routes: send / accept / reject ───────────────────────────────────


def _own(app, user):
    """Override require_user to *user*; returns a cleanup callable."""
    from app.dependencies import require_user

    app.dependency_overrides[require_user] = lambda: user
    return lambda: app.dependency_overrides.pop(require_user, None)


def test_send_route_flips_and_renders(client, db_session, owner, seller_company, priced_list):
    """POST …/bid/{id}/send emails + flips to sent and re-renders the tab (owner)."""
    from app.main import app

    _seed_site_email(db_session, seller_company, "buyer@initech.com")
    bid = _assemble(db_session, priced_list, owner)
    restore = _own(app, owner)
    try:
        with (
            patch("app.email_service.send_batch_rfq", new=AsyncMock(return_value=_sent_ok("buyer@initech.com"))),
            patch("app.services.document_service.generate_bid_report_pdf", return_value=b"%PDF stub"),
        ):
            resp = client.post(f"/api/resell/{priced_list.id}/bid/{bid.id}/send")
        assert resp.status_code == 200
        db_session.refresh(bid)
        assert bid.status == CustomerBidStatus.SENT
        assert "Mark accepted" in resp.text  # the sent-state action bar rendered
    finally:
        restore()


def test_accept_route(client, db_session, owner, priced_list):
    """POST …/bid/{id}/accept records acceptance (owner)."""
    from app.main import app

    bid = _sent_bid(db_session, priced_list, owner)
    restore = _own(app, owner)
    try:
        resp = client.post(f"/api/resell/{priced_list.id}/bid/{bid.id}/accept")
        assert resp.status_code == 200
        db_session.refresh(bid)
        assert bid.status == CustomerBidStatus.ACCEPTED
    finally:
        restore()


def test_reject_route(client, db_session, owner, priced_list):
    """POST …/bid/{id}/reject records rejection (owner)."""
    from app.main import app

    bid = _sent_bid(db_session, priced_list, owner)
    restore = _own(app, owner)
    try:
        resp = client.post(f"/api/resell/{priced_list.id}/bid/{bid.id}/reject")
        assert resp.status_code == 200
        db_session.refresh(bid)
        assert bid.status == CustomerBidStatus.REJECTED
    finally:
        restore()


def test_bid_route_owner_gated(client, db_session, owner, other_user, priced_list):
    """A non-owner acting on the bid is 403 (default client user ≠ owner)."""
    bid = _sent_bid(db_session, priced_list, owner)
    # The default client user (test_user, a buyer) is not the list owner.
    resp = client.post(f"/api/resell/{priced_list.id}/bid/{bid.id}/accept")
    assert resp.status_code == 403
