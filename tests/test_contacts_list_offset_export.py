"""tests/test_contacts_list_offset_export.py — customer-contacts workspace regressions.

C1 (stale-offset pagination): the #contacts-filters form must NOT round-trip an
offset field — a filter change always restarts at page 1, matching the accounts
sibling (_account_list.html: "the filter form itself intentionally carries no offset
field"); pagination links pin offset explicitly via hx-vals. Server-side,
customer_contacts_list_ctx snaps a stale beyond-range offset back to 0 instead of
rendering an empty page (defense-in-depth for direct/stale URLs).

C2 (ISS-028 leftover): contacts_list.html renders NO Export CSV control for ANY role
— PR #782 removed export controls from the vendors/requisitions/sightings/customers
toolbars but missed this partial. Bulk export UI lives only in the capability-gated
Settings "Data export" page; the /v2/customers/contacts/export.csv ROUTE itself is
unchanged (still gated on EXPORT_BULK_DATA — admin-by-default with per-user
override — covered by tests/test_export_bulk_data_gate.py).

Called by: pytest
Depends on: conftest (db_session, client, manager_client, test_user, test_company),
    app.services.crm_service.customer_contacts_list_ctx,
    app/templates/htmx/partials/customers/contacts_list.html
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import Company, CustomerSite, SiteContact, User

CONTACTS_LIST = Path("app/templates/htmx/partials/customers/contacts_list.html")


def _filter_form_block(src: str) -> str:
    """The #contacts-filters <form>…</form> block from the template source."""
    start = src.index('id="contacts-filters"')
    end = src.index("</form>", start)
    return src[start:end]


@pytest.fixture()
def owned_contacts(db_session: Session, test_company: Company, test_user: User) -> list[SiteContact]:
    """Two contacts under test_company, owned by test_user (visible to the buyer
    client)."""
    test_company.account_owner_id = test_user.id
    site = CustomerSite(company_id=test_company.id, site_name="HQ", site_type="hq", is_active=True)
    db_session.add(site)
    db_session.flush()
    contacts = []
    for name, email in (("Alice First", "alice@acme-electronics.com"), ("Bob Second", "bob@acme-electronics.com")):
        c = SiteContact(
            customer_site_id=site.id,
            full_name=name,
            email=email,
            is_active=True,
            created_at=datetime.now(UTC),
        )
        db_session.add(c)
        contacts.append(c)
    db_session.commit()
    for c in contacts:
        db_session.refresh(c)
    return contacts


# ── C1: filter changes must reset pagination ─────────────────────────────────


class TestFilterChangeResetsOffset:
    def test_filter_form_carries_no_offset_field(self):
        """The filter form must not round-trip offset — otherwise every filter change
        re-submits the stale offset and lands mid-list (often an empty page)."""
        block = _filter_form_block(CONTACTS_LIST.read_text(encoding="utf-8"))
        assert 'name="offset"' not in block, (
            "#contacts-filters must not carry an offset field — a filter change has to "
            "restart at page 1 (pagination links pin offset via hx-vals instead)"
        )

    def test_filter_form_still_carries_limit(self):
        """Page size is not page position — limit still round-trips with the filters."""
        block = _filter_form_block(CONTACTS_LIST.read_text(encoding="utf-8"))
        assert 'name="limit"' in block

    def test_pagination_links_pin_offset_via_hx_vals(self, client, owned_contacts):
        """Prev/Next still page: offset comes from hx-vals, filters from hx-include."""
        html = client.get("/v2/partials/contacts?limit=1").text
        assert '\'{"offset": "1"}\'' in html  # Next link
        assert 'hx-include="#contacts-filters"' in html

    def test_in_range_offset_still_respected(self, client, owned_contacts):
        """The server guard only snaps BEYOND-range offsets — real paging survives."""
        html = client.get("/v2/partials/contacts?limit=1&offset=1").text
        assert "of 2" in html
        assert "2&ndash;2 of 2" in html

    def test_route_snaps_beyond_range_offset_to_page_one(self, client, owned_contacts):
        """A stale offset past the (re)filtered result set renders page 1, not an empty
        page — the server never blindly trusts a round-tripped offset."""
        html = client.get("/v2/partials/contacts?offset=500").text
        assert "No contacts found" not in html
        assert "Alice First" in html
        assert "Bob Second" in html

    def test_ctx_snaps_beyond_range_offset(self, db_session, test_user, owned_contacts):
        """Service-level guard: offset >= total resets to 0 (shared by the GET route,
        the bulk-action re-render, and the edit-modal save re-render)."""
        from app.services.crm_service import customer_contacts_list_ctx

        ctx = customer_contacts_list_ctx(db_session, test_user, offset=500)
        assert ctx["offset"] == 0
        assert len(ctx["contacts"]) == 2


# ── C2: ISS-028 — no export control on the contacts list toolbar ─────────────


class TestContactsToolbarExportRemoved:
    def test_template_source_has_no_export_control(self):
        """Static guard: the partial must not reference the bulk export route or
        render an Export CSV control (ISS-028 — export UI lives only in the
        capability-gated Settings "Data export" page)."""
        src = CONTACTS_LIST.read_text(encoding="utf-8")
        assert "export.csv" not in src
        assert "Export CSV" not in src

    def test_export_button_hidden_for_buyer(self, client, owned_contacts):
        """ISS-028: a plain buyer never sees an Export CSV button on this toolbar."""
        html = client.get("/v2/partials/contacts").text
        assert "Export CSV" not in html
        assert "/v2/customers/contacts/export.csv" not in html

    def test_export_button_hidden_for_manager(self, manager_client):
        """ISS-028: no role sees export controls on the list toolbar — manager
        included (renders even with zero contacts; the toolbar is unconditional)."""
        html = manager_client.get("/v2/partials/contacts").text
        assert "Export CSV" not in html
        assert "/v2/customers/contacts/export.csv" not in html

    def test_accounts_link_survives(self, manager_client):
        """Removing the export anchor must not take the Accounts toolbar link with
        it."""
        html = manager_client.get("/v2/partials/contacts").text
        assert "Accounts" in html
        assert 'hx-get="/v2/partials/customers"' in html
