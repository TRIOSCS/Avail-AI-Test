"""Units 4 & 5 — open-vocab typeahead widget + essential/advanced facet fold."""

from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import seed_commodity_schemas


def test_open_vocab_facet_renders_typeahead_search(client, db_session: Session):
    seed_commodity_schemas(db_session)  # motherboards.chipset has no enum_values → open vocab
    # One card per chipset value (material_spec_facets is unique per card+spec_key).
    for i, s in enumerate(["Intel C621", "AMD X670"]):
        card = MaterialCard(normalized_mpn=f"mb-{i}", display_mpn=f"MB-{i}", category="motherboards")
        db_session.add(card)
        db_session.flush()
        db_session.add(
            MaterialSpecFacet(material_card_id=card.id, category="motherboards", spec_key="chipset", value_text=s)
        )
    db_session.commit()

    resp = client.get("/v2/partials/materials/filters/sub?commodity=motherboards&sub_filters=%7B%7D")
    assert resp.status_code == 200
    # Typeahead search box for the open-vocab "chipset" facet — state hoisted to the
    # parent component (ui.facetSearch[spec_key]) so it survives HTMX reloads.
    assert "ui.facetSearch['chipset']" in resp.text
    assert "Intel C621" in resp.text


def test_essential_facets_expanded_advanced_folded(client, db_session: Session):
    seed_commodity_schemas(db_session)  # hdd: capacity_gb + usage_class primary; rest advanced
    resp = client.get("/v2/partials/materials/filters/sub?commodity=hdd&sub_filters=%7B%7D")
    assert resp.status_code == 200
    # A "More filters (N)" fold exists for the non-primary facets...
    assert "More filters (" in resp.text
    # ...the primary Usage Class facet is present, and an advanced one (Interface) is too.
    assert "Usage Class" in resp.text
    assert "Interface" in resp.text
