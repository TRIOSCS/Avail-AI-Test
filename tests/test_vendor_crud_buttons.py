"""test_vendor_crud_buttons.py — CRM vendors surface: create-form duplicate check wiring
+ vendor-detail action-button targets.

Locks in two fixes:
1. The vendor create form's inline duplicate check calls the HTMX partial route
   ``GET /v2/partials/vendors/check-duplicate`` (HTML warning swapped into
   ``#dup-warning``) with the field the input actually sends (``display_name``) —
   NOT the JSON API route ``/api/vendors/check-duplicate?name=…``, which 422'd on
   every keystroke (param mismatch) and could never swap HTML anyway.
2. Vendor-detail action buttons (blacklist / archive / edit / delete) target the
   detail pane's own root id (``#vendor-detail-{id}``, ``hx-swap="outerHTML"``)
   instead of a hard-coded ``#main-content`` — so they work both standalone
   (detail inside ``#main-content``) and embedded (detail inside
   ``#crm-tab-content`` in the CRM shell) without nuking the shell. Same
   re-carried-container-id pattern as the task-edit fix (PR #781,
   ``customers/_task_edit_form.html``) and the company detail
   (``#company-detail-{id}``).

Called by: pytest
Depends on: conftest.py fixtures (client, db_session, test_vendor_card),
            app.routers.htmx.vendors,
            app/templates/htmx/partials/vendors/{create_form,detail,edit_vendor_form}.html
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import VendorCard

_HX = {"HX-Request": "true"}


# ── V1: create-form duplicate-check wiring ───────────────────────────────────


class TestCreateFormDupCheckWiring:
    def test_create_form_uses_htmx_partial_route(self, client: TestClient):
        resp = client.get("/v2/partials/vendors/create-form", headers=_HX)
        assert resp.status_code == 200
        assert 'hx-get="/v2/partials/vendors/check-duplicate"' in resp.text
        assert 'hx-target="#dup-warning"' in resp.text
        # The JSON API route rejects the form's display_name param with a 422 and
        # returns JSON that can't be swapped into #dup-warning — must not be wired.
        assert "/api/vendors/check-duplicate" not in resp.text


class TestVendorCheckDuplicatePartial:
    def test_exact_match_warns(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(
            "/v2/partials/vendors/check-duplicate",
            params={"display_name": test_vendor_card.display_name},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text
        assert test_vendor_card.display_name in resp.text

    def test_fuzzy_match_suggests(self, client: TestClient, test_vendor_card: VendorCard):
        # Transposed name — fuzzy suggestion (rapidfuzz fallback on SQLite).
        resp = client.get(
            "/v2/partials/vendors/check-duplicate",
            params={"display_name": "Arrow Electronisc"},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert "name match" in resp.text
        assert test_vendor_card.display_name in resp.text

    def test_no_match_renders_empty(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(
            "/v2/partials/vendors/check-duplicate",
            params={"display_name": "ZZZZZ Nonexistent Corp"},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_blank_name_renders_empty_not_422(self, client: TestClient):
        # Also locks route ordering: /check-duplicate must be registered BEFORE
        # /v2/partials/vendors/{vendor_id} or FastAPI 422s int-parsing the path.
        resp = client.get("/v2/partials/vendors/check-duplicate", headers=_HX)
        assert resp.status_code == 200
        assert resp.text == ""

    def test_escapes_html_in_matched_name(self, client: TestClient, db_session: Session):
        from app.vendor_utils import normalize_vendor_name

        name = 'Acme "Quoted" Components'
        db_session.add(
            VendorCard(
                normalized_name=normalize_vendor_name(name),
                display_name=name,
                sighting_count=0,
            )
        )
        db_session.commit()
        resp = client.get(
            "/v2/partials/vendors/check-duplicate",
            params={"display_name": name},
            headers=_HX,
        )
        assert resp.status_code == 200
        assert "&quot;Quoted&quot;" in resp.text
        assert '"Quoted"' not in resp.text


# ── V2: detail action buttons self-target the pane (embed + standalone) ─────


class TestVendorDetailActionTargets:
    def test_detail_root_has_stable_id(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}", headers=_HX)
        assert resp.status_code == 200
        assert f'id="vendor-detail-{test_vendor_card.id}"' in resp.text

    def test_standalone_actions_self_target_resolvable_in_pane(self, client: TestClient, test_vendor_card: VendorCard):
        """Standalone (#main-content) context: the four action buttons target the pane's
        own root id, which exists in the same fragment — so the swap replaces the pane
        in place and standalone behavior is preserved."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}", headers=_HX)
        assert resp.status_code == 200
        html = resp.text
        # blacklist + archive + edit + delete all self-target the pane root
        assert html.count(f'hx-target="#vendor-detail-{test_vendor_card.id}"') >= 4
        assert 'hx-swap="outerHTML"' in html
        assert f'id="vendor-detail-{test_vendor_card.id}"' in html
        assert 'hx-target="#main-content"' not in html

    def test_embed_context_actions_never_nuke_crm_shell(self, client: TestClient, test_vendor_card: VendorCard):
        """Embedded context: the CRM shell loads the vendor list with
        hx_target=#crm-tab-content; a row click swaps THIS SAME detail partial into
        #crm-tab-content. Its action buttons must not target #main-content — that
        would replace the whole CRM shell."""
        listing = client.get(
            "/v2/partials/vendors",
            params={"hx_target": "#crm-tab-content", "push_url_base": "/v2/crm"},
            headers=_HX,
        )
        assert listing.status_code == 200
        assert 'hx-target="#crm-tab-content"' in listing.text
        assert f"/v2/partials/vendors/{test_vendor_card.id}" in listing.text

        detail = client.get(f"/v2/partials/vendors/{test_vendor_card.id}", headers=_HX)
        assert detail.status_code == 200
        assert 'hx-target="#main-content"' not in detail.text

    def test_action_roundtrip_recarries_root_id(self, client: TestClient, test_vendor_card: VendorCard):
        """After an action re-renders the pane, the swapped-in fragment re-carries the
        pane root id so the NEXT action still resolves its target (the PR #781 re-
        carried-container-id pattern)."""
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/toggle-blacklist",
            headers=_HX,
        )
        assert resp.status_code == 200
        assert f'id="vendor-detail-{test_vendor_card.id}"' in resp.text
        assert 'hx-target="#main-content"' not in resp.text

    def test_archive_roundtrip_recarries_root_id(self, client: TestClient, test_vendor_card: VendorCard):
        resp = client.post(
            f"/v2/partials/vendors/{test_vendor_card.id}/archive",
            headers=_HX,
        )
        assert resp.status_code == 200
        assert f'id="vendor-detail-{test_vendor_card.id}"' in resp.text
        assert 'hx-target="#main-content"' not in resp.text

    def test_edit_form_recarries_root_id_and_self_targets(self, client: TestClient, test_vendor_card: VendorCard):
        """The Edit action outerHTML-replaces the pane with the edit form; the form root
        re-carries the pane id and Save/Cancel target it — in BOTH contexts."""
        resp = client.get(f"/v2/partials/vendors/{test_vendor_card.id}/edit-form", headers=_HX)
        assert resp.status_code == 200
        assert f'id="vendor-detail-{test_vendor_card.id}"' in resp.text
        assert f'hx-target="#vendor-detail-{test_vendor_card.id}"' in resp.text
        assert 'hx-swap="outerHTML"' in resp.text
        assert 'hx-target="#main-content"' not in resp.text
