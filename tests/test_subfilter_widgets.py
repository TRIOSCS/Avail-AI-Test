"""Units 4 & 5 — open-vocab typeahead widget + essential/advanced facet fold.

Also pins the seedless coarse-bucket contract: ics_other/oem_assemblies are clickable
sidebar entries with ZERO commodity_spec_schemas rows by design, so the sub-filter
partial must degrade to the explicit empty state rather than erroring.
"""

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.services.commodity_registry import COARSE_BUCKETS_WITHOUT_SEEDS, seed_commodity_schemas


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


def test_long_enum_facet_renders_search_within(client, db_session: Session):
    """A fixed-vocab enum facet with >12 values (connectors.connector_type has 19) gets
    a search-within box bound to the shared ui.facetSearch state, while a short enum
    facet on the same page (any hdd facet is <=6) gets no search box (P3)."""
    seed_commodity_schemas(db_session)

    resp = client.get("/v2/partials/materials/filters/sub?commodity=connectors&sub_filters=%7B%7D")
    assert resp.status_code == 200
    # The long connector_type facet (19 values) renders the search-within input...
    assert "ui.facetSearch['connector_type']" in resp.text
    assert "Search Connector Family / Type" in resp.text
    # ...and a short enum facet on the same page (gender, 3 values) does NOT.
    assert "ui.facetSearch['gender']" not in resp.text

    # A page whose enum facets are all short (hdd: every facet <=6) gets no search box at all.
    short = client.get("/v2/partials/materials/filters/sub?commodity=hdd&sub_filters=%7B%7D")
    assert short.status_code == 200
    assert "ui.facetSearch['interface']" not in short.text
    assert "ui.facetSearch['usage_class']" not in short.text


@pytest.mark.parametrize("commodity", sorted(COARSE_BUCKETS_WITHOUT_SEEDS))
def test_coarse_bucket_subfilters_render_empty_state(client, db_session: Session, commodity):
    """The first-ever seedless tree commodities (declared coarse buckets) must render
    the explicit 'no filters' branch — users WILL click them in the sidebar because the
    forward hook and migration 093 funnel real cards into these buckets."""
    seed_commodity_schemas(db_session)  # seeds everything EXCEPT the coarse buckets
    # A real card in the bucket, exactly as migration 093 / the forward hook produce.
    db_session.add(MaterialCard(normalized_mpn=f"cb-{commodity}", display_mpn=f"CB-{commodity}", category=commodity))
    db_session.commit()

    resp = client.get(f"/v2/partials/materials/filters/sub?commodity={commodity}&sub_filters=%7B%7D")
    assert resp.status_code == 200
    assert "No filters available" in resp.text


def test_essential_facets_expanded_advanced_folded(client, db_session: Session):
    seed_commodity_schemas(db_session)  # hdd: capacity_gb + usage_class primary; rest advanced
    resp = client.get("/v2/partials/materials/filters/sub?commodity=hdd&sub_filters=%7B%7D")
    assert resp.status_code == 200
    # A "More filters (N)" fold exists for the non-primary facets...
    assert "More filters (" in resp.text
    # ...the primary Usage Class facet is present, and an advanced one (Interface) is too.
    assert "Usage Class" in resp.text
    assert "Interface" in resp.text
