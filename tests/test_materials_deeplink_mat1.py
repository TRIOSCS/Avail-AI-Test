"""MAT-1 — a /v2/materials/{id} deep-link must lazy-load the CARD DETAIL, not the list.

Regression (2026-07-02 production-polish audit): "materials" was missing from
_DETAIL_VIEWS in htmx_views.py, so v2_page dropped the {id} path segment and the shell
lazy-loaded /v2/partials/materials (the faceted list). Effect: pressing F5 on an open
card, following a row's pushed /v2/materials/{id} URL, or the Add-part HX-Redirect all
dumped the user back on the list instead of the card. Adding "materials" to _DETAIL_VIEWS
routes the shell at /v2/partials/materials/{id} (material_detail_partial).

v2_page authenticates via get_user(request, db) directly (not a Depends the client
fixture overrides), so these tests patch app.routers.htmx_views.get_user to the seeded
buyer — otherwise the route returns the login page (which is also 200, the reason the
older == 200 page tests can't catch this).

Called by: pytest
Depends on: app.routers.htmx_views v2_page, base_page.html (hx-get="{{ partial_url }}").
"""

from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.intelligence import MaterialCard


def _seed_card(db: Session) -> MaterialCard:
    card = MaterialCard(normalized_mpn="lm317t", display_mpn="LM317T", manufacturer="TI", search_count=0)
    db.add(card)
    db.commit()
    db.refresh(card)
    return card


def test_materials_deeplink_lazy_loads_detail(client: TestClient, db_session: Session, test_user):
    """/v2/materials/{id} shell must point its lazy-load at the detail partial."""
    card = _seed_card(db_session)

    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        html = client.get(f"/v2/materials/{card.id}").text

    # The shell's single lazy-load div (base_page.html) must fetch the DETAIL partial.
    assert f'hx-get="/v2/partials/materials/{card.id}"' in html
    # ...and NOT the faceted list (closing quote disambiguates the /{id} suffix).
    assert 'hx-get="/v2/partials/materials"' not in html


def test_materials_index_still_lazy_loads_list(client: TestClient, test_user):
    """The bare /v2/materials index must still lazy-load the faceted list, unchanged."""
    with patch("app.routers.htmx_views.get_user", return_value=test_user):
        html = client.get("/v2/materials").text
    assert 'hx-get="/v2/partials/materials"' in html
