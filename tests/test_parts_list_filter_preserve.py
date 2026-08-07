"""test_parts_list_filter_preserve.py — Sales-Hub parts list must preserve the active
search + status/sort/dir/archived across sort/pill/pagination clicks.

Regression guard for the dead-control bug where the ``_fp()`` filter-params
helper omitted ``q``, so sort headers, status pills and Prev/Next dropped the
active search term. All list controls build their query from the Alpine filter
state (``pQ`` / ``pStatus`` / ``pSort`` / ``pDir`` / ``pArchived``) via a single
``_fp()`` source of truth. (The inline-edit ``_reload()`` machinery died with
the read-only triage conversion — spec §5.1 — edits happen on the requisition
detail page now.)

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


def test_fp_includes_search_term_and_all_filter_state(client, db_session, test_user):
    """_fp() — the one query source — includes q plus status/sort/dir/archived."""
    html = _render(client, db_session, test_user)
    assert "q: this.pQ || undefined" in html
    assert "status: this.pStatus" in html
    assert "sort: this.pSort" in html
    assert "dir: this.pDir" in html
    assert "include_archived: this.pArchived" in html


def test_search_term_is_tracked_filter_state(client, db_session, test_user):
    """PQ (search) is Alpine state wired to the input: seeded from its server-rendered
    value and kept reactive on every keystroke (so pill/sort/page hx-vals stay
    fresh)."""
    html = _render(client, db_session, test_user)
    assert "pQ:" in html
    assert 'x-init="pQ = $el.value"' in html
    assert '@input="pQ = $event.target.value"' in html


def test_pagination_carries_filter_state(client, db_session, test_user):
    """Prev/Next buttons build their query via _fp({offset: ...}) so paging never drops
    the active search/status/sort."""
    req = _make_requisition(db_session, test_user)
    for i in range(51):  # limit is 50 — force a second page
        _make_requirement(db_session, req.id, f"PAGE-PART-{i:03d}")
    resp = client.get("/v2/partials/parts")
    assert resp.status_code == 200
    assert "_fp({offset: 50})" in resp.text
