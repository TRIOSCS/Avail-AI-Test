"""test_sightings_resell_density.py — two density fixes.

Fix 1 (Sightings board toolbar): the "Pending" dashboard counter was a duplicate
       control — it filtered status=offered, exactly like the "Offered" status pill.
       The counter is removed; the Offered pill and the Urgent/Stale counters remain.
Fix 2 (Resell workspace triage strip): the top stat tiles were oversized. They were
       condensed (tighter padding, smaller value, less chrome) WITHOUT changing their
       filter behavior — the one-click needs=/stage= links and per-token active-ring
       bindings survive the size reduction.

These are render-based tests: they drive the real partial endpoints and assert on the
rendered HTML (no router logic changed).

Called by: pytest
Depends on: app/templates/htmx/partials/sightings/table.html,
            app/templates/htmx/partials/resell/workspace.html, tests/conftest.py
"""

import os

os.environ["TESTING"] = "1"

from sqlalchemy.orm import Session


def _seed_requirement(db: Session, user_id: int, mpn: str = "DENSITY-MPN"):
    """Seed one requisition + requirement so the full sightings board renders."""
    from app.models import Requirement, Requisition

    req = Requisition(name="Density Req", status="open", created_by=user_id)
    db.add(req)
    db.flush()
    requirement = Requirement(
        requisition_id=req.id,
        primary_mpn=mpn,
        normalized_mpn=mpn.upper(),
        target_qty=10,
        sourcing_status="open",
    )
    db.add(requirement)
    db.commit()
    return requirement


# ── Fix 1: sightings toolbar drops the duplicate Pending counter ──────


def test_sightings_toolbar_drops_duplicate_pending_counter(client, db_session, test_user):
    """The redundant "Pending" dashboard counter (it filtered status=offered, identical
    to the Offered pill) no longer renders — its label and blue counter chrome are
    gone."""
    _seed_requirement(db_session, test_user.id)

    resp = client.get("/v2/partials/sightings")
    assert resp.status_code == 200
    body = resp.text

    assert "Pending" not in body  # the duplicate counter's visible label is gone
    assert "bg-blue-500" not in body  # ...and its blue status-dot chrome went with it


def test_sightings_toolbar_keeps_offered_pill_and_other_counters(client, db_session, test_user):
    """The surviving controls stay: the Offered status pill (now the single status=offered
    control) plus the untouched Urgent and Stale dashboard counters."""
    _seed_requirement(db_session, test_user.id)

    body = client.get("/v2/partials/sightings").text

    # Exactly one control filters status=offered now (the pill) — the duplicate is gone.
    assert body.count("status=offered") == 1
    assert "Offered" in body  # the Offered status pill label
    # The other dashboard counters are untouched.
    assert "Urgent" in body
    assert "Stale" in body


# ── Fix 2: resell triage tiles keep their filters after condensing ────


def test_resell_triage_tiles_keep_filter_links(client, db_session, test_user):
    """The condensed triage tiles keep their real one-click filter links — the offer-
    based needs= queries and the status-based stage= queries all survive the size
    reduction."""
    resp = client.get("/v2/partials/resell/workspace")
    assert resp.status_code == 200
    body = resp.text

    # Offer-based tiles keep their needs= filters ...
    assert "needs=offers" in body
    assert "needs=take_all" in body
    # ... and the status tiles keep their stage= filters.
    assert "stage=open" in body
    assert "stage=bid_out" in body
    assert "stage=awarded" in body
    # No tile regressed to an empty stage value (the old dead-control bug).
    assert '&stage="' not in body


def test_resell_triage_tiles_active_ring_bindings_intact(client, db_session, test_user):
    """Each tile still highlights on its own unique token, so the active-ring keeps
    working after the tiles shrank."""
    body = client.get("/v2/partials/resell/workspace").text

    for token in ("open", "offers", "take_all", "bid_out", "awarded"):
        assert f"filter === '{token}'" in body
    assert "ring-2 ring-accent-400" in body


def test_resell_triage_tiles_are_condensed(client, db_session, test_user):
    """The tiles physically shrank: the oversized shared-card value size (text-2xl) no
    longer renders in the strip, and the condensed value size (text-lg) is in place — a
    size reduction, not a behavior change."""
    body = client.get("/v2/partials/resell/workspace").text

    assert "text-2xl" not in body  # shared stat_card's oversized value is gone
    assert "text-lg" in body  # condensed tile value size is present
