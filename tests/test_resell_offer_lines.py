"""test_resell_offer_lines.py — R1: offer modal renders posting lines + honest match
state.

Covers the multi-line offer submission funnel (Phase-3 resell, Task 3): the offer form
lists every posting line as a checkbox + qty + unit-price row (``line_item_ids`` — one
checked value per line — plus per-line ``qty_<id>`` / ``price_<id>`` fields) plus one
optional free-text row for a part NOT on the posting. ONE POST to
``/api/resell/{list_id}/offers`` still produces ONE ExcessOffer (per_line scope) with
one ExcessOfferLine per submitted row — the SAME model shape
``excess_service.submit_offer`` already builds; each checked line's OWN part_number
rides as ``mpn_raw`` through the existing part-number-only matcher
(``_classify_mpn_match``), so it matches itself, while the free-text row lands
unmatched unless it happens to normalize onto a posted line. The response toast
(server-owned, via ``set_toast``) states the honest match outcome — replacing the old
unconditional client-side "Offer submitted". The trader's own-offers view
(``_offers.html``) surfaces a match-status chip on any unmatched/ambiguous line of
THEIR OWN offer.

Called by: pytest. Depends on: conftest fixtures (client auths as test_user, a buyer),
app.routers.resell, app.services.excess_service.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.constants import ExcessListStatus
from app.models import Company, User
from app.models.excess import ExcessLineItem, ExcessList, ExcessOffer, ExcessOfferLine
from app.utils.normalization import normalize_mpn_key


@pytest.fixture()
def owner_user(db_session: Session) -> User:
    """The list owner — a trader, distinct from the buyer client (test_user)."""
    user = User(
        email="ol-owner@trioscs.com",
        name="Ol Owner",
        role="trader",
        azure_id="ol-azure-owner",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _posted_list_with_lines(db_session: Session, owner: User, company: Company, parts: list[str]) -> ExcessList:
    el = ExcessList(
        title="Offer-lines posting",
        company_id=company.id,
        owner_id=owner.id,
        status=ExcessListStatus.COLLECTING,
        created_at=datetime.now(UTC),
    )
    db_session.add(el)
    db_session.flush()
    for pn in parts:
        db_session.add(
            ExcessLineItem(
                excess_list_id=el.id,
                part_number=pn,
                normalized_part_number=normalize_mpn_key(pn),
                quantity=100,
                condition="New",
            )
        )
    db_session.commit()
    db_session.refresh(el)
    return el


@pytest.fixture()
def posted_list(db_session: Session, owner_user: User, test_company: Company) -> ExcessList:
    """A posted (collecting) list with TWO lines, owned by owner_user."""
    return _posted_list_with_lines(db_session, owner_user, test_company, ["LM358N", "MAX232"])


def _toast_message(resp) -> str:
    return json.loads(resp.headers["HX-Trigger"])["showToast"]["message"]


# ── (a) GET offer form renders each posting line's MPN + checkbox/qty/price fields ──


def test_offer_form_renders_posting_lines(client, db_session, posted_list, owner_user, test_user):
    assert test_user.id != owner_user.id
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-form")
    assert resp.status_code == 200
    body = resp.text
    assert "LM358N" in body
    assert "MAX232" in body

    lines = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).all()
    assert len(lines) == 2
    for li in lines:
        assert f'name="line_item_ids" value="{li.id}"' in body
        assert f'name="qty_{li.id}"' in body
        assert f'name="price_{li.id}"' in body


# ── (b) multi-line submit persists offers/lines with correct qty+price mapping ──────


def test_multi_line_submit_persists_correct_qty_and_price(client, db_session, posted_list, owner_user, test_user):
    l1, l2 = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).order_by(ExcessLineItem.id).all()

    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={
            "scope": "per_line",
            "line_item_ids": [str(l1.id), str(l2.id)],
            f"qty_{l1.id}": "10",
            f"price_{l1.id}": "1.5000",
            f"qty_{l2.id}": "20",
            f"price_{l2.id}": "2.7500",
        },
    )
    assert resp.status_code == 200

    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id, submitted_by=test_user.id).one()
    assert offer.scope == "per_line"
    persisted = {ol.mpn_raw: ol for ol in db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).all()}
    assert len(persisted) == 2

    assert persisted[l1.part_number].quantity == 10
    assert persisted[l1.part_number].unit_price == Decimal("1.5000")
    assert persisted[l1.part_number].match_status == "matched"
    assert persisted[l1.part_number].excess_line_item_id == l1.id

    assert persisted[l2.part_number].quantity == 20
    assert persisted[l2.part_number].unit_price == Decimal("2.7500")
    assert persisted[l2.part_number].match_status == "matched"
    assert persisted[l2.part_number].excess_line_item_id == l2.id

    assert _toast_message(resp) == "Matched 2 lines"


def test_checked_line_without_valid_qty_is_skipped_not_persisted(
    client, db_session, posted_list, owner_user, test_user
):
    """A checked box with a blank/zero qty drops that ONE row — it does not reject the
    whole submission (the other checked line still lands)."""
    l1, l2 = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).order_by(ExcessLineItem.id).all()

    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={
            "scope": "per_line",
            "line_item_ids": [str(l1.id), str(l2.id)],
            f"qty_{l1.id}": "10",
            f"price_{l1.id}": "1.00",
            f"qty_{l2.id}": "0",  # invalid — this row is skipped
        },
    )
    assert resp.status_code == 200
    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id, submitted_by=test_user.id).one()
    persisted = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).all()
    assert len(persisted) == 1
    assert persisted[0].mpn_raw == l1.part_number


# ── (c) free-text part not on the list → unmatched, and the response names it ──────


def test_free_text_row_lands_unmatched_and_toast_names_it(client, db_session, posted_list, owner_user, test_user):
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": "NOTONLIST-999", "quantity": "5", "unit_price": "3.00"},
    )
    assert resp.status_code == 200

    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id, submitted_by=test_user.id).one()
    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "unmatched"
    assert line.excess_line_item_id is None
    assert line.mpn_raw == "NOTONLIST-999"  # queued, never dropped

    assert _toast_message(resp) == "Matched 0 lines — 1 not on the posting, flagged unmatched"


def test_mixed_checked_and_free_text_toast_counts_both(client, db_session, posted_list, owner_user, test_user):
    l1 = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).order_by(ExcessLineItem.id).first()

    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={
            "scope": "per_line",
            "line_item_ids": [str(l1.id)],
            f"qty_{l1.id}": "10",
            "mpn_raw": "SOMETHING-ELSE",
            "quantity": "2",
        },
    )
    assert resp.status_code == 200
    assert _toast_message(resp) == "Matched 1 line — 1 not on the posting, flagged unmatched"


# ── (d) own-offers table shows the unmatched chip (and none for a matched line) ────


def test_own_offers_view_shows_unmatched_chip(client, db_session, posted_list, owner_user, test_user):
    client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": "NOTONLIST-777", "quantity": "3"},
    )
    body = client.get(f"/v2/partials/resell/{posted_list.id}/offers").text
    assert "NOTONLIST-777" in body
    assert "not on the posting" in body


def test_own_offers_view_matched_line_has_no_unmatched_chip(client, db_session, posted_list, owner_user, test_user):
    l1 = db_session.query(ExcessLineItem).filter_by(excess_list_id=posted_list.id).order_by(ExcessLineItem.id).first()
    client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={
            "scope": "per_line",
            "line_item_ids": [str(l1.id)],
            f"qty_{l1.id}": "10",
            f"price_{l1.id}": "1.00",
        },
    )
    body = client.get(f"/v2/partials/resell/{posted_list.id}/offers").text
    assert l1.part_number in body
    assert "not on the posting" not in body
    assert "ambiguous match" not in body


# ── (e) identity hiding intact: the form for a non-owner shows no customer identity ─


def test_offer_form_hides_customer_identity(client, posted_list, owner_user, test_user):
    """The header uses the anonymized ``display_title``, never the seller-named free-
    text title.

    (The separate buyer-attribution <select> intentionally lists
    every CRM company — including, coincidentally, the seller — as generic
    ATTRIBUTION options; that pre-existing, out-of-scope design is not the identity
    leak this gate guards. See offer_form.html's ``companies`` docstring note.)
    """
    assert test_user.id != owner_user.id
    resp = client.get(f"/v2/partials/resell/{posted_list.id}/offer-form")
    assert resp.status_code == 200
    body = resp.text
    assert posted_list.title not in body  # the seller-named free-text title never leaks
    assert f'On <span class="font-medium text-gray-700">Excess listing #{posted_list.id}</span>' in body


# ── Guards on the new multi-line parsing ────────────────────────────────────────────


def test_no_selection_and_no_free_text_returns_400(client, posted_list, owner_user, test_user):
    resp = client.post(f"/api/resell/{posted_list.id}/offers", data={"scope": "per_line"})
    assert resp.status_code == 400


def test_checked_id_from_another_list_is_ignored(client, db_session, posted_list, owner_user, test_company, test_user):
    """A checked line_item_id belonging to a DIFFERENT list is never trusted blind —
    dropped silently, so with nothing else selected the "pick at least one" guard fires
    and nothing is persisted."""
    other_list = _posted_list_with_lines(db_session, owner_user, test_company, ["OTHERPART"])
    foreign_line = db_session.query(ExcessLineItem).filter_by(excess_list_id=other_list.id).one()

    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "line_item_ids": [str(foreign_line.id)], f"qty_{foreign_line.id}": "5"},
    )
    assert resp.status_code == 400
    assert (
        db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id, submitted_by=test_user.id).count() == 0
    )


# ── Control: take_all scope still works, with its own server toast ─────────────────


def test_take_all_toast_message(client, posted_list, owner_user, test_user):
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "take_all", "take_all_total_price": "500.00"},
    )
    assert resp.status_code == 200
    assert _toast_message(resp) == "Take-all offer submitted"


# ── Control: the pre-existing single-field free-text-only submit path is unchanged ──


def test_legacy_single_field_submit_still_matches(client, db_session, posted_list, owner_user, test_user):
    """Pre-R1 callers that only ever posted mpn_raw/quantity/unit_price (no
    line_item_ids) still work — the free-text row IS the whole per-line matcher path
    when nothing is checked."""
    resp = client.post(
        f"/api/resell/{posted_list.id}/offers",
        data={"scope": "per_line", "mpn_raw": "LM358N", "quantity": "10", "unit_price": "5.00"},
    )
    assert resp.status_code == 200
    offer = db_session.query(ExcessOffer).filter_by(excess_list_id=posted_list.id, submitted_by=test_user.id).one()
    line = db_session.query(ExcessOfferLine).filter_by(offer_id=offer.id).one()
    assert line.match_status == "matched"
    assert _toast_message(resp) == "Matched 1 line"
