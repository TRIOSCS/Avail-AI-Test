"""B6 (workflow review): the requisitions list surfaces each req's aggregate quote
status.

Guards the new Quotes column: the header is present, a requisition with a quote shows its
status badge, and — critically — the header <th> count matches every data row's <td> count
(both role-based column orders stay aligned).

Called by: pytest
Depends on: requisitions/list.html + req_row.html, list_requisitions (quote_status), bs4.
"""

import os

os.environ["TESTING"] = "1"

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


def _list_soup(client: TestClient) -> BeautifulSoup:
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200
    return BeautifulSoup(resp.text, "html.parser")


def test_quotes_column_header_present(client: TestClient, test_requisition):
    soup = _list_soup(client)
    headers = [th.get_text(strip=True) for th in soup.select("thead th")]
    assert "Quotes" in headers


def test_row_cell_count_matches_header(client: TestClient, test_requisition):
    """Header/row alignment: adding the Quotes column must keep every data row's <td>
    count equal to the header <th> count (the two role orders were edited in
    lockstep)."""
    soup = _list_soup(client)
    th_count = len(soup.select("thead th"))
    rows = soup.select('tr[id^="req-row-"]')
    assert rows, "expected at least one requisition row"
    for tr in rows:
        tds = tr.find_all("td", recursive=False)
        assert len(tds) == th_count, f"row has {len(tds)} cells, header has {th_count}"


def test_quoted_req_shows_status_badge(client: TestClient, db_session, test_customer_site, test_user):
    """A requisition with a sent quote shows the quote status badge (not the empty
    dash).

    Uses a clean req + one quote so the priority-ordered quote_status is unambiguous.
    """
    from datetime import datetime, timezone

    from app.models import Quote, Requisition

    req = Requisition(
        name="B6-QUOTED",
        customer_name="Quoted Co",
        status="open",
        customer_site_id=test_customer_site.id,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    db_session.add(
        Quote(
            requisition_id=req.id,
            customer_site_id=test_customer_site.id,
            quote_number="Q-B6-SENT",
            status="sent",
            line_items=[],
            created_by_id=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    row = _list_soup(client).select_one(f'tr[id="req-row-{req.id}"]')
    assert row is not None
    assert "Sent" in row.get_text()  # quote_status_badge label for 'sent'


def _make_req_with_quote(db_session, site, user, name, quote_status=None, n_quotes=1):
    from datetime import datetime, timezone

    from app.models import Quote, Requisition

    req = Requisition(
        name=name,
        customer_name=f"{name} Co",
        status="open",
        customer_site_id=site.id,
        created_by=user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    db_session.flush()
    if quote_status:
        for i in range(n_quotes):
            db_session.add(
                Quote(
                    requisition_id=req.id,
                    customer_site_id=site.id,
                    quote_number=f"Q-{name}-{i}",
                    status=quote_status,
                    line_items=[],
                    created_by_id=user.id,
                    created_at=datetime.now(timezone.utc),
                )
            )
    db_session.commit()
    return req


def test_quote_status_sort_orders_by_significance(client: TestClient, db_session, test_customer_site, test_user):
    """The Quotes column header renders a sort link, so ?sort=quote_status must actually
    sort (won > lost > sent > revised, no-quote rows last) instead of silently falling
    back to created_at (the #623 regression: 'quote_status' was missing from the route's
    sort whitelist)."""
    # Created oldest-first so a silent created_at fallback (desc default → newest first,
    # asc → oldest first) CANNOT accidentally produce the expected significance order.
    won = _make_req_with_quote(db_session, test_customer_site, test_user, "SORT-NONE-DECOY", None)
    sent = _make_req_with_quote(db_session, test_customer_site, test_user, "SORT-SENT", "sent")
    winner = _make_req_with_quote(db_session, test_customer_site, test_user, "SORT-WON", "won")

    resp = client.get("/v2/partials/requisitions?sort=quote_status&dir=asc")
    assert resp.status_code == 200
    text = resp.text
    pos_won, pos_sent, pos_none = (text.find(f"req-row-{r.id}") for r in (winner, sent, won))
    assert pos_won != -1 and pos_sent != -1 and pos_none != -1
    assert pos_won < pos_sent < pos_none, "expected won < sent < no-quote row order under quote_status asc"


def test_inline_save_rerender_keeps_quote_status(client: TestClient, db_session, test_customer_site, test_user):
    """Inline-saving any field returns the full row — it must keep the Quotes cell
    populated (the #623 regression: the row-context re-render never computed
    req.quote_status, degrading the cell to the dash)."""
    req = _make_req_with_quote(db_session, test_customer_site, test_user, "INLINE-KEEP", "sent")

    resp = client.patch(
        f"/v2/partials/requisitions/{req.id}/inline",
        data={"field": "name", "value": "INLINE-KEEP-RENAMED", "context": "row"},
    )
    assert resp.status_code == 200
    assert "Sent" in resp.text, "row re-render lost the quote_status badge"


def test_multi_quote_req_renders_once(client: TestClient, db_session, test_customer_site, test_user):
    """A requisition with several quotes/requirements/offers must render exactly one row
    (guards entity dedup across the collection-loader strategy)."""
    req = _make_req_with_quote(db_session, test_customer_site, test_user, "MULTI-Q", "sent", n_quotes=3)

    soup = _list_soup(client)
    rows = soup.select(f'tr[id="req-row-{req.id}"]')
    assert len(rows) == 1
