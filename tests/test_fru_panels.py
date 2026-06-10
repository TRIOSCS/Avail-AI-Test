"""Router/template tests for the FRU crosswalk panels and lookup endpoint.

What: Asserts the material detail surface renders the "FRU matrix" panel (forward
      view), the "Used in FRUs" panel (reverse view), and stays clean for parts with
      no crosswalk data; and that /v2/partials/materials/fru-lookup serves both views
      plus an empty state (only for a non-empty query), rejects unauthenticated
      callers, and is not shadowed by the {card_id} route.
Called by: pytest
Depends on: conftest client fixture, app.models (MaterialCard, FruLink),
            app/templates/htmx/partials/materials/fru_section.html
"""

from app.constants import FruLinkKind
from app.models import FruLink, MaterialCard


def _seed_link(db, fru="00AJ001", related="ST9300603SS", kind=FruLinkKind.MFG_MODEL, **attrs):
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


def _seed_card(db, mpn):
    card = MaterialCard(normalized_mpn=mpn.lower(), display_mpn=mpn)
    db.add(card)
    db.commit()
    return card


class TestDetailSurface:
    def test_fru_matrix_panel_renders_for_fru_card(self, client, db_session):
        card = _seed_card(db_session, "00AJ001")
        _seed_link(db_session, manufacturer="Seagate", qual_status="qlot approved")

        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "FRU matrix" in resp.text
        assert "ST9300603SS" in resp.text
        assert "Seagate" in resp.text
        assert "qlot approved" in resp.text

    def test_used_in_frus_panel_renders_for_related_pn(self, client, db_session):
        card = _seed_card(db_session, "ST9300603SS")
        _seed_link(db_session, fru="00AJ001", related="ST9300603SS")
        _seed_link(db_session, fru="42D0638", related="ST9300603SS")

        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "Used in FRUs" in resp.text
        assert "00AJ001" in resp.text
        assert "42D0638" in resp.text
        assert "Manufacturer model" in resp.text

    def test_no_panels_without_crosswalk_data(self, client, db_session):
        card = _seed_card(db_session, "LM358DR")

        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert resp.status_code == 200
        assert "FRU matrix" not in resp.text
        assert "Used in FRUs" not in resp.text
        # The detail surface itself still renders.
        assert "LM358DR" in resp.text

    def test_cdc_pending_badge(self, client, db_session):
        card = _seed_card(db_session, "00AK334")
        _seed_link(
            db_session,
            fru="00AK334",
            related="00D5331",
            kind=FruLinkKind.DRIVE_PN,
            qual_status="cdc_pending",
        )
        resp = client.get(f"/v2/partials/materials/{card.id}")
        assert "CDC pending" in resp.text


class TestFruLookupEndpoint:
    def test_forward_view(self, client, db_session):
        _seed_link(db_session, manufacturer="Seagate")
        resp = client.get("/v2/partials/materials/fru-lookup?q=00AJ001")
        assert resp.status_code == 200
        assert "FRU matrix" in resp.text
        assert "ST9300603SS" in resp.text

    def test_reverse_view(self, client, db_session):
        _seed_link(db_session)
        resp = client.get("/v2/partials/materials/fru-lookup?q=ST9300603SS")
        assert resp.status_code == 200
        assert "Used in FRUs" in resp.text
        assert "00AJ001" in resp.text

    def test_input_normalized(self, client, db_session):
        _seed_link(db_session)
        resp = client.get("/v2/partials/materials/fru-lookup?q=%2000-aj-001%20")
        assert resp.status_code == 200
        assert "FRU matrix" in resp.text

    def test_empty_state(self, client, db_session):
        resp = client.get("/v2/partials/materials/fru-lookup?q=NOPE999")
        assert resp.status_code == 200
        assert "No FRU crosswalk data" in resp.text

    def test_no_empty_state_for_blank_query(self, client, db_session):
        # show_empty=bool(q): a blank lookup must not render the confusing
        # "No FRU crosswalk data for ." empty state.
        resp = client.get("/v2/partials/materials/fru-lookup?q=")
        assert resp.status_code == 200
        assert "No FRU crosswalk data" not in resp.text

    def test_not_shadowed_by_card_id_route(self, client, db_session):
        # If the {card_id} route captured this path it would 422 on int coercion.
        resp = client.get("/v2/partials/materials/fru-lookup?q=")
        assert resp.status_code == 200

    def test_unauthenticated_rejected(self, unauthenticated_client):
        # require_user must stay on the endpoint — the crosswalk is sourcing
        # intelligence; the authed `client` fixture overrides it, so this is the
        # only test exercising the dependency.
        resp = unauthenticated_client.get("/v2/partials/materials/fru-lookup?q=00AJ001")
        assert resp.status_code in (401, 403)
