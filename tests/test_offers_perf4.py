"""PERF-4 — list_offers must resolve substitute MPNs to MaterialCards in ONE batch
query, not one point lookup per substitute per requirement.

Regression (2026-07-02 production-polish audit): the cross-requisition historical-offer
block ran `db.query(MaterialCard.id).filter_by(normalized_mpn=sub_key).first()` inside a
per-substitute loop nested in a per-requirement loop → N×M point queries on every
offers-tab render. The fix batches all substitute keys into a single
`MaterialCard.normalized_mpn IN (...)` query keyed on normalized_mpn (which is unique),
so the result is byte-for-byte identical.

This file guards both properties:
  - test_substitute_lookup_query_count_independent_of_substitute_count — the N+1 guard
  - test_substitute_resolution_matches_point_lookup_behavior — behavioral equivalence,
    including the empty / whitespace / None / dict / duplicate / no-match edge cases.

Called by: pytest
Depends on: app.routers.crm.offers.list_offers (GET /api/requisitions/{id}/offers).
"""

import os

os.environ["TESTING"] = "1"
os.environ["RATE_LIMIT_ENABLED"] = "false"

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import event

from app.models import Offer, Requirement, Requisition
from app.models.intelligence import MaterialCard


class _QueryCounter:
    """Count SQL statements executed on the session's engine within a `with` block."""

    def __init__(self, db):
        self.engine = db.get_bind()
        self.count = 0

    def _on_exec(self, *a, **k):
        self.count += 1

    def __enter__(self):
        event.listen(self.engine, "after_cursor_execute", self._on_exec)
        return self

    def __exit__(self, *a):
        event.remove(self.engine, "after_cursor_execute", self._on_exec)


def _card(db, normalized_mpn: str) -> MaterialCard:
    card = MaterialCard(normalized_mpn=normalized_mpn, display_mpn=normalized_mpn.upper())
    db.add(card)
    db.flush()
    return card


def _req_with_substitutes(db, owner, *, substitutes, material_card_id=None) -> Requisition:
    """A requisition holding one requirement with the given substitutes JSON."""
    req = Requisition(
        name="REQ-PERF4",
        customer_name="Acme",
        status="open",
        created_by=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(req)
    db.flush()
    item = Requirement(
        requisition_id=req.id,
        primary_mpn="PRIMARYMPN",
        target_qty=100,
        material_card_id=material_card_id,
        substitutes=substitutes,
        created_at=datetime.now(timezone.utc),
    )
    db.add(item)
    db.commit()
    db.refresh(req)
    return req


def test_substitute_lookup_query_count_independent_of_substitute_count(client, db_session, test_user):
    """Six substitutes must cost the same number of SQL statements as one (no N+1).

    Under the old per-substitute `.first()` loop, six substitutes issued five extra
    point queries than one. The batched IN(...) query makes the count constant.
    """
    req1 = _req_with_substitutes(db_session, test_user, substitutes=["SUB-A0001"])
    with _QueryCounter(db_session) as c1:
        r1 = client.get(f"/api/requisitions/{req1.id}/offers")
    assert r1.status_code == 200
    one = c1.count

    six_subs = [f"SUB-B{i:04d}" for i in range(6)]
    req6 = _req_with_substitutes(db_session, test_user, substitutes=six_subs)
    with _QueryCounter(db_session) as c6:
        r6 = client.get(f"/api/requisitions/{req6.id}/offers")
    assert r6.status_code == 200
    six = c6.count

    assert six == one, f"substitute lookup N+1: 1 sub={one} queries, 6 subs={six}"


def test_substitute_resolution_matches_point_lookup_behavior(client, db_session, test_user):
    """The batched resolution must surface exactly the same historical offers as the
    per-substitute point lookup did — including all the edge cases.

    Requirement carries:
      - material_card_id -> card_primary (cross-req historical offer H_primary)
      - substitutes: one string mapping to an existing card (card_sub -> H_sub),
        plus a dict, empty string, whitespace, None, a duplicate of the matching
        string, and a string that normalizes to a key with no MaterialCard.
    Only H_primary (is_substitute=False) and H_sub (is_substitute=True) may appear,
    each exactly once; the same-requisition offer must NOT appear as historical.
    """
    card_primary = _card(db_session, "primarympn")
    card_sub = _card(db_session, "subexist")  # normalize_mpn_key("SUB-EXIST") -> "subexist"

    req = _req_with_substitutes(
        db_session,
        test_user,
        material_card_id=card_primary.id,
        substitutes=[
            "SUB-EXIST",
            {"mpn": "IGNORED-DICT"},  # non-string -> skipped
            "",  # empty -> skipped
            "   ",  # whitespace -> skipped
            None,  # None -> skipped
            "SUB-EXIST",  # duplicate -> single card, single historical offer
            "SUB-NONE",  # normalizes to "subnone", no MaterialCard -> skipped
        ],
    )

    # A DIFFERENT requisition owns the historical offers (cross-req join).
    other = Requisition(
        name="REQ-OTHER",
        customer_name="Beta",
        status="open",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other)
    db_session.flush()

    def _hist_offer(card_id, vendor):
        o = Offer(
            requisition_id=other.id,
            material_card_id=card_id,
            vendor_name=vendor,
            mpn=vendor,
            qty_available=10,
            unit_price=Decimal("1.00"),
            status="active",
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(o)
        db_session.flush()
        return o

    h_primary = _hist_offer(card_primary.id, "PrimaryHistVendor")
    h_sub = _hist_offer(card_sub.id, "SubHistVendor")
    # Same-requisition offer on the primary card — must be excluded from historical.
    same_req_offer = Offer(
        requisition_id=req.id,
        requirement_id=req.requirements[0].id,
        material_card_id=card_primary.id,
        vendor_name="SameReqVendor",
        mpn="SameReqVendor",
        qty_available=5,
        unit_price=Decimal("2.00"),
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(same_req_offer)
    db_session.commit()

    resp = client.get(f"/api/requisitions/{req.id}/offers")
    assert resp.status_code == 200
    data = resp.json()

    groups = {g["requirement_id"]: g for g in data["groups"]}
    hist = groups[req.requirements[0].id]["historical_offers"]

    by_id = {h["id"]: h for h in hist}
    assert set(by_id) == {h_primary.id, h_sub.id}, f"unexpected historical set: {set(by_id)}"
    assert len(hist) == 2, f"duplicate/missing historical offers: {[h['id'] for h in hist]}"
    assert by_id[h_primary.id]["is_substitute"] is False
    assert by_id[h_sub.id]["is_substitute"] is True
    assert same_req_offer.id not in by_id  # same-req offer is never historical
