"""test_offer_vendor_dup_nudge.py — TDD tests for the offer-form vendor dup nudge
(survey idea #10).

The offer form's vendor_name was a bare input — the biggest source of vendor-row
fragmentation. A blur-check reuses the deterministic vendor_duplicates matcher
(exact normalized + pg_trgm fuzzy — NO AI on the hot path) and renders a
"Did you mean Arrow Electronics?" nudge with a one-tap adopt that swaps ONLY the
input's name string (never blocks save, never re-links rows).

Covers: the GET /v2/partials/offers/vendor-dup-check endpoint (exact/fuzzy nudge
with an adopt button carrying the canonical name in an autoescaped data attribute;
empty on blank/no-match), and the offer form field wiring (hx blur trigger + id +
#vendor-dup-nudge target).

Called by: pytest (TESTING=1 PYTHONPATH=. pytest tests/test_offer_vendor_dup_nudge.py -v)
Depends on: app.routers.htmx.offers.crud, conftest (client, db_session, test_user).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import User
from app.models.sourcing import Requisition
from app.models.vendors import VendorCard

_HX = {"HX-Request": "true"}


def _vendor(db: Session, display: str, norm: str) -> VendorCard:
    vc = VendorCard(normalized_name=norm, display_name=display)
    db.add(vc)
    db.commit()
    return vc


def _req(db: Session, user: User) -> Requisition:
    r = Requisition(
        name="VDN-REQ", customer_name="Acme", status="open", created_by=user.id, created_at=datetime.now(UTC)
    )
    db.add(r)
    db.commit()
    return r


class TestNudgeEndpoint:
    _URL = "/v2/partials/offers/vendor-dup-check"

    def test_exact_match_gives_no_nudge_on_offer_form(self, client, db_session, test_user):
        """On an OFFER form an exact match is the normal case (logging an offer against
        an existing vendor — no fragmentation risk), so it must NOT nudge.

        This also kills the post-adopt re-nudge wrinkle (adopting → exact → silent).
        """
        _vendor(db_session, "Arrow Electronics", "arrow electronics")
        resp = client.get(self._URL, params={"vendor_name": "Arrow Electronics"}, headers=_HX)
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_fuzzy_match_renders_adopt_nudge(self, client, db_session, test_user):
        _vendor(db_session, "Arrow Electronics", "arrow electronics")
        resp = client.get(self._URL, params={"vendor_name": "Arow Electronic"}, headers=_HX)  # near-miss
        assert resp.status_code == 200
        body = resp.text
        assert "Arrow Electronics" in body
        assert 'data-name="Arrow Electronics"' in body  # autoescaped canonical name to adopt
        assert "@click" in body  # tappable adopt

    def test_no_match_returns_empty(self, client, db_session, test_user):
        _vendor(db_session, "Arrow Electronics", "arrow electronics")
        resp = client.get(self._URL, params={"vendor_name": "Zorblax Unlimited"}, headers=_HX)
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_blank_returns_empty_without_query(self, client, db_session, test_user):
        resp = client.get(self._URL, params={"vendor_name": "   "}, headers=_HX)
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_adopt_name_is_attribute_escaped(self, client, db_session, test_user):
        """A vendor display name with a quote can't break out of the data attribute.

        Query a FUZZY near-miss so the nudge actually renders the malicious name.
        """
        _vendor(db_session, 'Ac"me <x> Corp', "acme x corp")
        resp = client.get(self._URL, params={"vendor_name": "Acme x Corpp"}, headers=_HX)  # near-miss → fuzzy
        assert resp.status_code == 200
        assert resp.text.strip()  # the nudge did render (fuzzy hit on the malicious name)
        # raw double-quote/angle brackets never appear unescaped in the attribute
        assert 'data-name="Ac"me' not in resp.text
        assert "<x>" not in resp.text

    def test_unauthenticated_401(self, unauthenticated_client, db_session):
        resp = unauthenticated_client.get(self._URL, params={"vendor_name": "Arrow"}, headers=_HX)
        assert resp.status_code == 401


def test_offer_form_field_wires_the_blur_check(client, db_session, test_user):
    r = _req(db_session, test_user)
    resp = client.get(f"/v2/partials/requisitions/{r.id}/add-offer-form", headers=_HX)
    assert resp.status_code == 200
    body = resp.text
    assert "/v2/partials/offers/vendor-dup-check" in body
    assert 'id="offer-vendor-name"' in body
    assert 'id="vendor-dup-nudge"' in body
    assert "blur" in body  # hx-trigger fires on blur, not per keystroke
