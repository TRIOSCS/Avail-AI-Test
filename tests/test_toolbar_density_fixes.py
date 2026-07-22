"""test_toolbar_density_fixes.py — Render-based coverage for the toolbar-density
tightening of the Prospecting and CRM-Accounts top bars.

Pins three condensations (no information/functional loss — controls only get folded
onto fewer rows / relabelled, never removed):

  Prospecting (app/templates/htmx/partials/prospecting/list.html)
    - The scope segmented control is relabelled All/Mine -> Everyone/Mine so it no
      longer collides with the "All" status pill on the same row.
    - The search input now shares the pills/sort toolbar row (id="prospect-toolbar")
      instead of sitting alone on a full-width row; the result count moves up next to
      the page title (id="prospect-count", above the stats panel).

  CRM Accounts (app/templates/htmx/partials/customers/list.html)
    - "+ New account" folds onto the SAME row as "+ Save view" (id="cdm-utility-bar")
      instead of a separate full-width row below the VIEWS row.

Each case also asserts the key controls all still render (no loss).

Called by: pytest autodiscovery
Depends on: conftest.py fixtures (client, manager_client, db_session, test_company)
"""

import os

os.environ["TESTING"] = "1"

_HX = {"HX-Request": "true"}


# ── Prospecting: scope relabel (Everyone/Mine, not a duplicate "All") ─────────


class TestProspectingScopeRelabel:
    def test_scope_control_reads_everyone_mine_not_all(self, client, db_session):
        html = client.get("/v2/partials/prospecting").text
        # The scope segmented control lives in the role="group" aria-label="Scope" box.
        i = html.index('aria-label="Scope"')
        scope = html[i : i + 1000]
        assert "Everyone" in scope  # relabelled from "All"
        assert "Mine" in scope
        # It still round-trips both scope values (no functional loss).
        assert "scope=all" in html
        assert "scope=mine" in html

    def test_scope_carrier_and_toggle_preserved(self, client, db_session):
        html = client.get("/v2/partials/prospecting?scope=mine").text
        assert 'name="scope" value="mine"' in html


# ── Prospecting: search shares the pills/sort row; count next to title ────────


class TestProspectingToolbarRow:
    def test_search_shares_pills_sort_row(self, client, db_session):
        html = client.get("/v2/partials/prospecting").text
        bar = html.index('id="prospect-toolbar"')
        # The search input (only double-quoted name="q" on the page) sits INSIDE the
        # shared toolbar row, not on an earlier standalone full-width row.
        q = html.index('name="q"')
        assert q > bar
        # The sort control shares the same row.
        assert html.index('name="sort"') > bar

    def test_result_count_sits_by_title_above_stats(self, client, db_session):
        html = client.get("/v2/partials/prospecting").text
        # Count moved up next to the page title -> it renders before the stats panel
        # (previously it sat on its own row between stats and the grid).
        assert 'id="prospect-count"' in html
        assert html.index('id="prospect-count"') < html.index('id="prospect-stats"')

    def test_no_control_loss(self, client, db_session):
        html = client.get("/v2/partials/prospecting").text
        # All status filter pills.
        for label in ("Suggested", "Claimed", "Converted", "Dismissed", "Expired"):
            assert label in html
        # Search + sort options + add-prospect all still present.
        assert 'name="q"' in html
        assert "AI match (best first)" in html
        assert "Most buyer-ready" in html
        assert "Add prospect" in html


# ── CRM Accounts: "+ New account" folds onto the "+ Save view" row ────────────


class TestAccountsUtilityRow:
    def test_new_account_and_save_view_share_one_row(self, client, test_company):
        html = client.get("/v2/partials/customers", headers=_HX).text
        bar = html.index('id="cdm-utility-bar"')
        # The saved-views wrapper (with "+ Save view") is nested inside the single
        # utility bar, and "+ New account" lives on that same bar — no separate row.
        assert html.index('id="saved-views-customers"') > bar
        assert html.index("+ New account") > bar
        assert html.index("+ Save view") > bar

    def test_new_account_button_still_modal_wired(self, client, test_company):
        html = client.get("/v2/partials/customers", headers=_HX).text
        assert "+ New account" in html
        assert "/v2/partials/customers/create-form" in html
        assert 'hx-target="#modal-content"' in html

    def test_no_utility_control_loss(self, manager_client, test_company):
        # ISS-028: Export CSV / Export contacts moved to the capability-gated
        # (EXPORT_BULK_DATA, admin-by-default) Settings "Data export" page —
        # no export UI on the list toolbar for ANY role,
        # including manager. The remaining (non-export) utilities survive the fold.
        html = manager_client.get("/v2/partials/customers", headers=_HX).text
        # Saved views + new + non-export utilities all survive the fold.
        assert "+ Save view" in html
        assert "All contacts" in html
        assert "Import CSV" in html
        # Filter controls untouched.
        assert 'id="cdm-search"' in html
        assert 'name="staleness"' in html
        assert 'name="account_type"' in html
        assert 'name="sort"' in html
        assert 'name="disposition"' in html
        assert "My accounts" in html
        assert "Has open reqs" in html

    def test_export_csv_hidden_for_all_roles(self, client, manager_client, test_company):
        """ISS-028: bulk export controls never appear on the list toolbar for ANY
        role (buyer or manager) — the only export UI is the capability-gated
        Settings "Data export" page."""
        for c in (client, manager_client):
            html = c.get("/v2/partials/customers", headers=_HX).text
            assert "Export CSV" not in html
            assert "Export contacts" not in html
            assert "Import CSV" in html
            assert "All contacts" in html
