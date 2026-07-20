"""Regression guards for the 4 functionality-sim defects (2026-07-20).

D1/D2: Alpine @htmx:after-request must reset the form via $el.reset(), not
       this.reset() (this = component scope, not the form -> TypeError).
D3:    /v2/customers must deterministically auto-load the first account's detail
       via an htmx load trigger (not a racy Alpine x-init click).
D4:    the manufacturer "add new" typeahead option must read q from a single-quoted
       data- attr, never inline {{ q|tojson }} into the double-quoted onclick
       (tojson's quotes close the attribute -> 'Unexpected token }').
"""

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.models import Company, User

TPL = Path("app/templates/htmx/partials")


class TestFormResetHandlers:
    """D1 + D2: the @htmx (Alpine) reset handlers use $el.reset(), never
    this.reset()."""

    @pytest.mark.parametrize(
        "path",
        [
            "customers/tabs/sites_tab.html",
            "vendors/tabs/contacts.html",
        ],
    )
    def test_alpine_reset_uses_el_not_this(self, path):
        src = (TPL / path).read_text()
        assert "@htmx:after-request" in src
        # The Alpine binding must not call this.reset() (fails silently, form never closes).
        assert "this.reset()" not in src, f"{path}: Alpine @htmx handler still uses this.reset()"
        assert "$el.reset()" in src, f"{path}: expected $el.reset() in the Alpine reset handler"


class TestManufacturerAddNewOnclick:
    """D4: the add-new option must not inline q|tojson into the double-quoted onclick."""

    def test_add_new_reads_q_from_data_attr(self):
        src = (TPL / "manufacturers/search_results.html").read_text()
        # The safe pattern: single-quoted data attr + JSON.parse in the handler.
        assert "data-mfr-q='{{ q|tojson }}'" in src
        assert "JSON.parse(this.dataset.mfrQ)" in src
        # The broken pattern (tojson inlined into the onclick) must be gone.
        assert "qVal={{ q|tojson }}" not in src

    def test_add_new_option_renders_wellformed(self, client, db_session: Session, test_user: User):
        resp = client.get("/v2/partials/manufacturers/search?q=Zephyr%20Semi")
        assert resp.status_code == 200
        body = resp.text
        assert "as new manufacturer" in body
        assert "JSON.parse(this.dataset.mfrQ)" in body
        # No attribute-breaking bare quote: the literal onclick with an inlined "Zephyr..." must not appear.
        assert 'qVal="Zephyr' not in body


class TestCustomersAutoLoadFirstAccount:
    """D3: the detail pane auto-loads the first account via an htmx load trigger."""

    def test_detail_pane_has_load_trigger_when_companies_exist(self, client, db_session: Session, test_user: User):
        company = Company(name="AutoLoad Co", is_active=True, account_owner_id=test_user.id)
        db_session.add(company)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        body = resp.text
        # The #cdm-detail loader must target the detail with a deterministic load trigger.
        assert 'hx-trigger="load"' in body
        assert f"/v2/partials/customers/{company.id}" in body
