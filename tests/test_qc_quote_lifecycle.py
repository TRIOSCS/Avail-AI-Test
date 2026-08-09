"""tests/test_qc_quote_lifecycle.py — QC 2026-08-08 cluster 2 regressions.

Two confirmed criticals: (1) next_quote_number trusted the newest ROW, so
revising a non-latest quote (which re-issues its canonical number on a new
row) wedged every subsequent create into a unique-constraint 500; (2) the
HTMX quote delete lacked the JSON path's buy-plan guard, so reopen-to-draft
then delete CASCADE-destroyed the buy plan, quality plan, and PAID
prepayments.

Called by: pytest autodiscovery
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import UTC, datetime

from app.models import BuyPlan, Quote, Requisition
from app.services.crm_service import next_quote_number
from tests.conftest import engine  # noqa: F401

YEAR = datetime.now(UTC).year


def _req(db, user):
    r = Requisition(name="qc-quote-req", status="quoted", created_by=user.id)
    db.add(r)
    db.flush()
    return r


def _quote(db, req, number, status="draft"):
    q = Quote(requisition_id=req.id, quote_number=number, line_items=[], status=status)
    db.add(q)
    db.commit()
    return q


# ── next_quote_number: max-sequence, never newest-row ────────────────────


def test_next_number_survives_revising_a_non_latest_quote(db_session, test_user):
    """The wedge: revise #1 while #2 exists → newest row carries #1's canonical
    number again; the generator must still produce #3, not collide on #2."""
    req = _req(db_session, test_user)
    q1 = _quote(db_session, req, f"Q-{YEAR}-0001", status="sent")
    _quote(db_session, req, f"Q-{YEAR}-0002", status="sent")

    # Simulate the revise flow: old row renamed to -R1, canonical number
    # re-issued on a NEW (newest-id) row.
    q1.quote_number = f"Q-{YEAR}-0001-R1"
    db_session.commit()
    _quote(db_session, req, f"Q-{YEAR}-0001", status="draft")

    assert next_quote_number(db_session) == f"Q-{YEAR}-0003"


def test_next_number_parses_past_revision_suffixes(db_session, test_user):
    """A ...-R{n} row as the only/latest match must neither crash the parse nor reset
    the sequence to 1."""
    req = _req(db_session, test_user)
    _quote(db_session, req, f"Q-{YEAR}-0005-R1", status="sent")
    assert next_quote_number(db_session) == f"Q-{YEAR}-0006"


def test_next_number_empty_year_starts_at_one(db_session):
    assert next_quote_number(db_session) == f"Q-{YEAR}-0001"


def test_next_number_repeated_calls_never_collide_after_revision(db_session, test_user):
    """Regression for the permanent-wedge symptom: creating quotes after a
    revision must keep working, not 500 on every retry."""
    req = _req(db_session, test_user)
    for i in range(1, 4):
        _quote(db_session, req, f"Q-{YEAR}-{i:04d}", status="sent")
    q1 = db_session.query(Quote).filter(Quote.quote_number == f"Q-{YEAR}-0001").one()
    q1.quote_number = f"Q-{YEAR}-0001-R1"
    db_session.commit()
    _quote(db_session, req, f"Q-{YEAR}-0001", status="draft")

    n1 = next_quote_number(db_session)
    _quote(db_session, req, n1, status="draft")
    n2 = next_quote_number(db_session)
    assert (n1, n2) == (f"Q-{YEAR}-0004", f"Q-{YEAR}-0005")


# ── HTMX quote delete: buy-plan guard ────────────────────────────────────


def test_htmx_delete_blocked_when_buy_plan_linked(client, db_session, test_user):
    """Reopen-to-draft then delete must NOT cascade into the buy plan chain."""
    req = _req(db_session, test_user)
    quote = _quote(db_session, req, f"Q-{YEAR}-0900", status="draft")
    plan = BuyPlan(
        quote_id=quote.id,
        requisition_id=req.id,
        status="active",
        so_status="approved",
        created_at=datetime.now(UTC),
    )
    db_session.add(plan)
    db_session.commit()

    resp = client.delete(f"/v2/partials/quotes/{quote.id}")
    assert resp.status_code == 400
    assert "linked buy plans" in resp.text
    assert db_session.get(Quote, quote.id) is not None
    assert db_session.get(BuyPlan, plan.id) is not None  # chain intact


def test_htmx_delete_still_works_without_buy_plan(client, db_session, test_user):
    req = _req(db_session, test_user)
    quote = _quote(db_session, req, f"Q-{YEAR}-0901", status="draft")
    resp = client.delete(f"/v2/partials/quotes/{quote.id}")
    assert resp.status_code == 200
    assert resp.headers.get("HX-Redirect") == "/v2/requisitions"
    assert db_session.get(Quote, quote.id) is None
