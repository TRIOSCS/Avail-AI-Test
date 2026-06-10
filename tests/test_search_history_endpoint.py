"""Tests for GET /v2/partials/search/history — the search-page history panel.

Covers the part-history states (found / empty / error / unauthenticated) and the
compact FRU-crosswalk context card: forward hit (searched MPN is a FRU — summary
counts + top mfg models), reverse hit ("Used in N FRUs" distinct count + top FRU
numbers), both-direction hits, the materials-surface deep link, silence when
fru_links has no match, scoped degradation when only the FRU lookups fail, and
the softened empty-history copy for crosswalk-known parts.

Called by: pytest.
Depends on: part_history_service, fru_matrix_service, the search_history_panel
            route, history_panel.html.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import FruLinkKind
from app.models import FruLink
from app.models.intelligence import MaterialCard
from app.models.offers import Offer
from app.models.sourcing import Requisition


def _seed_fru_link(db, fru="00AJ001", related="ST9300603SS", kind=FruLinkKind.MFG_MODEL, **attrs):
    link = FruLink(
        fru_raw=fru,
        fru_norm=fru.lower(),
        related_raw=related,
        related_norm=related.lower(),
        rel_kind=kind.value,
        source_sheet=attrs.pop("source_sheet", "Main"),
        **attrs,
    )
    db.add(link)
    db.commit()
    return link


def test_unknown_mpn_returns_empty_state(client: TestClient, db_session: Session):
    resp = client.get("/v2/partials/search/history?mpn=NOSUCHPART", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "looks new" in resp.text.lower()


def test_known_mpn_renders_history(client: TestClient, db_session: Session):
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)
    req = Requisition(name="R", customer_name="ACME", status="active")
    db_session.add(req)
    db_session.commit()
    db_session.refresh(req)
    db_session.add(
        Offer(
            requisition_id=req.id,
            material_card_id=card.id,
            vendor_name="Avnet",
            mpn="LM317T",
            qty_available=10,
            unit_price=Decimal("4.1"),
            status="active",
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    # "LM-317T" normalizes to the same key "lm317t" as the stored card.
    resp = client.get("/v2/partials/search/history?mpn=LM-317T", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Avnet" in resp.text  # offer rendered
    assert f"/v2/materials/{card.id}" in resp.text  # deep link to full part page


def test_service_failure_renders_error_panel_not_500(client: TestClient, db_session: Session, monkeypatch):
    """A failure in get_part_history degrades to a 200 error panel, never a 500 or the
    misleading 'looks new' empty state."""
    import app.services.part_history_service as svc

    def _boom(*_a, **_k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(svc, "get_part_history", _boom)
    resp = client.get("/v2/partials/search/history?mpn=LM317T", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "could not be loaded" in resp.text.lower()
    assert "looks new" not in resp.text.lower()  # failure must NOT masquerade as empty
    assert "FRU crosswalk" not in resp.text  # error panel carries no crosswalk context


@pytest.mark.parametrize("failing_fn", ["get_fru_view", "get_reverse_context"])
def test_fru_context_failure_keeps_history_card(client: TestClient, db_session: Session, monkeypatch, failing_fn: str):
    """The FRU crosswalk card is ADDITIVE: a crosswalk lookup failure degrades to
    'no crosswalk card' — it must never discard a successfully loaded history or
    render the amber history-error panel."""
    import app.services.fru_matrix_service as fru_svc

    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db_session.add(card)
    db_session.commit()
    db_session.refresh(card)

    def _boom(*_a, **_k):
        raise RuntimeError("fru_links exploded")

    monkeypatch.setattr(fru_svc, failing_fn, _boom)
    resp = client.get("/v2/partials/search/history?mpn=LM317T", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert f"/v2/materials/{card.id}" in resp.text  # history card still renders
    assert "could not be loaded" not in resp.text.lower()  # no false error panel
    assert "FRU crosswalk" not in resp.text  # crosswalk card silently degrades


class TestFruCrosswalkContext:
    """Compact FRU-crosswalk card in the 'What we know' panel."""

    def test_forward_hit_renders_summary_models_and_deep_link(self, client: TestClient, db_session: Session):
        """Searched MPN IS a FRU → count summary + top mfg models + materials deep
        link."""
        _seed_fru_link(db_session, fru="00AJ001", related="ST9300603SS", manufacturer="Seagate")
        _seed_fru_link(db_session, fru="00AJ001", related="00VN562", kind=FruLinkKind.DRIVE_PN)
        _seed_fru_link(db_session, fru="00AJ001", related="68Y7789", kind=FruLinkKind.IBM_11S)
        _seed_fru_link(db_session, fru="00AJ001", related="44T2216", kind=FruLinkKind.TRAY)

        resp = client.get("/v2/partials/search/history?mpn=00AJ001", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "FRU crosswalk" in resp.text
        assert "is a FRU" in resp.text
        assert "1 drive PN · 1 model · 1 11S number · 1 tray" in resp.text
        # Top mfg-model chip with its manufacturer.
        assert "ST9300603SS" in resp.text
        assert "Seagate" in resp.text
        # Deep link to the materials surface (href + HTMX nav, same q pattern as fru_section).
        assert "View full FRU matrix" in resp.text
        assert 'href="/v2/materials?q=00AJ001"' in resp.text
        assert 'hx-get="/v2/partials/materials?q=00AJ001"' in resp.text

    def test_forward_hit_caps_model_chips_at_three(self, client: TestClient, db_session: Session):
        for i in range(4):
            _seed_fru_link(db_session, fru="00AJ001", related=f"ST930060{i}SS")

        resp = client.get("/v2/partials/search/history?mpn=00AJ001", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        # Items sort alphabetically (none qualified) — first 3 shown, 4th omitted.
        for i in range(3):
            assert f"ST930060{i}SS" in resp.text
        assert "ST9300603SS" not in resp.text

    def test_reverse_hit_renders_used_in_frus_and_deep_link(self, client: TestClient, db_session: Session):
        """Searched MPN appears UNDER FRUs → 'Used in N FRUs' + top FRU numbers."""
        _seed_fru_link(db_session, fru="00AJ001", related="ST9300603SS", manufacturer="Seagate")
        _seed_fru_link(db_session, fru="42D0638", related="ST9300603SS")

        resp = client.get("/v2/partials/search/history?mpn=ST9300603SS", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "FRU crosswalk" in resp.text
        assert "Used in" in resp.text
        assert "FRUs" in resp.text
        assert "00AJ001" in resp.text
        assert "42D0638" in resp.text
        assert "is a FRU" not in resp.text  # no forward section on a reverse-only hit
        assert 'href="/v2/materials?q=ST9300603SS"' in resp.text

    def test_reverse_hit_caps_fru_chips_at_three(self, client: TestClient, db_session: Session):
        for i in range(1, 6):
            _seed_fru_link(db_session, fru=f"00AJ00{i}", related="44T2216", kind=FruLinkKind.TRAY)

        resp = client.get("/v2/partials/search/history?mpn=44T2216", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert ">5</span>" in resp.text  # uncapped distinct-FRU count
        for fru in ("00AJ001", "00AJ002", "00AJ003"):
            assert fru in resp.text
        assert "00AJ004" not in resp.text
        assert "00AJ005" not in resp.text

    def test_reverse_hit_counts_distinct_frus_not_roles(self, client: TestClient, db_session: Session):
        """One FRU under two roles + one other FRU = 'Used in 2 FRUs', matching the FRU-
        deduplicated chips (NOT 3, the (FRU, role) usage count)."""
        _seed_fru_link(db_session, fru="00AJ001", related="44T2216", kind=FruLinkKind.TRAY)
        _seed_fru_link(db_session, fru="00AJ001", related="44T2216", kind=FruLinkKind.TRAY_ALT)
        _seed_fru_link(db_session, fru="00AJ002", related="44T2216", kind=FruLinkKind.TRAY)

        resp = client.get("/v2/partials/search/history?mpn=44T2216", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert ">2</span>" in resp.text
        assert ">3</span>" not in resp.text

    def test_both_direction_hit_renders_forward_and_reverse_sections(self, client: TestClient, db_session: Session):
        """A part that IS a FRU and ALSO appears under other FRUs (e.g. a nested
        assembly) renders both sections in the one crosswalk card."""
        _seed_fru_link(db_session, fru="00AJ001", related="ST9300603SS")
        _seed_fru_link(db_session, fru="42D0638", related="00AJ001", kind=FruLinkKind.ASSEMBLY)

        resp = client.get("/v2/partials/search/history?mpn=00AJ001", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "FRU crosswalk" in resp.text
        assert "is a FRU" in resp.text  # forward section
        assert "Used in" in resp.text  # reverse section
        assert "42D0638" in resp.text

    def test_crosswalk_only_part_softens_empty_history_copy(self, client: TestClient, db_session: Session):
        """A crosswalk-known part without trading history must not claim to 'look new to
        us' directly above a card proving we know it."""
        _seed_fru_link(db_session, fru="00AJ001", related="ST9300603SS")

        resp = client.get("/v2/partials/search/history?mpn=00AJ001", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "FRU crosswalk" in resp.text
        assert "No trading history yet" in resp.text
        assert "looks new" not in resp.text.lower()

    def test_no_hit_renders_nothing(self, client: TestClient, db_session: Session):
        """No fru_links match → the panel stays silent (no FRU card at all)."""
        _seed_fru_link(db_session, fru="00AJ001", related="ST9300603SS")

        resp = client.get("/v2/partials/search/history?mpn=LM317T", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert "FRU crosswalk" not in resp.text
        assert "View full FRU matrix" not in resp.text

    def test_crosswalk_card_coexists_with_history_card(self, client: TestClient, db_session: Session):
        """A part with BOTH internal history and crosswalk data shows both cards."""
        card = MaterialCard(normalized_mpn="st9300603ss", display_mpn="ST9300603SS", search_count=0)
        db_session.add(card)
        db_session.commit()
        _seed_fru_link(db_session, fru="00AJ001", related="ST9300603SS")

        resp = client.get("/v2/partials/search/history?mpn=ST9300603SS", headers={"HX-Request": "true"})
        assert resp.status_code == 200
        assert f"/v2/materials/{card.id}" in resp.text  # history card deep link
        assert "FRU crosswalk" in resp.text


def test_unauthenticated_request_blocked(db_session: Session):
    """The endpoint requires a logged-in user."""
    from app.database import get_db
    from app.main import app

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    try:
        with TestClient(app, raise_server_exceptions=False) as anon:
            resp = anon.get("/v2/partials/search/history?mpn=LM317T")
        assert resp.status_code in (401, 403, 302)
    finally:
        app.dependency_overrides.pop(get_db, None)
