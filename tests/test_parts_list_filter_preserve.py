"""test_parts_list_filter_preserve.py — Sales-Hub parts list must preserve the active
search + status/sort/dir/archived + page across inline edits and sort/pill/pagination
clicks.

Regression guard for the dead-control bug where:
  * the ``@part-updated`` / ``@part-archived`` reload rebuilt the URL from
    ``window.location.search`` (always empty — no control sets ``hx-push-url``),
    resetting the search (``q``), status, sort and page to defaults; and
  * the ``_fp()`` filter-params helper omitted ``q``, so sort headers, status
    pills and Prev/Next dropped the active search term.

Both now build the query from the Alpine filter state (``pQ`` / ``pStatus`` /
``pSort`` / ``pDir`` / ``pArchived`` / ``pOffset``) via a single ``_fp()`` source
of truth, and the reload goes through ``_reload()``.

Renders GET /v2/partials/parts and asserts on the fixed template expressions.
Depends on: conftest fixtures (client, db_session, test_user).
"""

from app.constants import SourcingStatus
from app.models import Requirement, Requisition


def _make_requisition(db, user, name="REQ-FILTER"):
    req = Requisition(name=name, customer_name="Acme", status="open", created_by=user.id)
    db.add(req)
    db.commit()
    db.refresh(req)
    return req


def _make_requirement(db, req_id, mpn, status=SourcingStatus.OPEN):
    r = Requirement(requisition_id=req_id, primary_mpn=mpn, sourcing_status=status)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _render(client, db, user):
    req = _make_requisition(db, user)
    _make_requirement(db, req.id, "PART-FILTER")
    resp = client.get("/v2/partials/parts")
    assert resp.status_code == 200
    return resp.text


def test_reload_no_longer_reads_empty_location_search(client, db_session, test_user):
    """The always-empty window.location.search reload is gone."""
    html = _render(client, db_session, test_user)
    assert "window.location.search" not in html


def test_reload_handlers_go_through_reload_helper(client, db_session, test_user):
    """@part-updated / @part-archived reload via _reload() (filter-state query)."""
    html = _render(client, db_session, test_user)
    assert '@part-updated.window="_reload()"' in html
    assert '@part-archived.window="_reload()"' in html


def test_reload_builds_query_from_filter_state_and_preserves_page(client, db_session, test_user):
    """_reload() fetches with _fp() values (incl.

    current page offset), not the URL.
    """
    html = _render(client, db_session, test_user)
    # GET the list, values built from the single _fp() source of truth, with
    # the current page (pOffset) carried so an inline edit stays on the page.
    assert "htmx.ajax('GET', '/v2/partials/parts'" in html
    assert "values: JSON.parse(this._fp({offset: this.pOffset}))" in html
    # Loading indicator on the new imperative call (static-analysis contract).
    assert "indicator: '#parts-list'" in html


def test_fp_includes_search_term_and_all_filter_state(client, db_session, test_user):
    """_fp() — the one query source — includes q plus status/sort/dir/archived."""
    html = _render(client, db_session, test_user)
    assert "q: this.pQ || undefined" in html
    assert "status: this.pStatus" in html
    assert "sort: this.pSort" in html
    assert "dir: this.pDir" in html
    assert "include_archived: this.pArchived" in html


def test_search_term_and_offset_are_tracked_filter_state(client, db_session, test_user):
    """PQ (search) and pOffset (page) are Alpine state; pQ is wired to the input."""
    html = _render(client, db_session, test_user)
    assert "pQ:" in html
    assert "pOffset:" in html
    # Search input is the source of pQ: seeded from its server-rendered value
    # and kept reactive on every keystroke (so pill/sort/page hx-vals stay fresh).
    assert 'x-init="pQ = $el.value"' in html
    assert '@input="pQ = $event.target.value"' in html
