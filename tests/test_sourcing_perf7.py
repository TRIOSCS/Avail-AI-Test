"""PERF-7 — the sourcing leads partials must resolve each lead's best sighting in ONE
batched query, not a per-lead N+1, and must return byte-for-byte the same data.

Regression (2026-07-02 production-polish audit): three textually identical per-lead loops
(results partial, workspace, workspace-list) each ran
``db.query(Sighting).filter(requirement_id==.., vendor_name_normalized==lead.norm)
.order_by(created_at.desc().nullslast()).first()`` inside a Python ``for`` over up to 24
leads — up to 24 extra round-trips per render. They now share ``_lead_sighting_data``.

This suite pins BOTH properties:
  (a) query count is independent of lead count (no N+1);
  (b) the helper's output is identical to the old per-lead loop across the multi-row edge
      cases (empty, no-sighting vendor, null metrics, null created_at ordering, and two
      leads sharing one vendor).

Called by: pytest
Depends on: app.routers.htmx.sourcing._lead_sighting_data.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition, Sighting, User
from app.models.sourcing_lead import SourcingLead
from app.routers.htmx.sourcing import _lead_sighting_data


# ── query counter (mirrors tests/test_proactive_perf2.py) ────────────────────
class _QueryCounter:
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


# ── reference implementation = the ORIGINAL per-lead N+1 loop, verbatim ───────
def _old_lead_sighting_data(db: Session, requirement_id: int, leads: list) -> dict:
    """The pre-PERF-7 per-lead loop, kept here as the equivalence oracle."""
    lead_sighting_data: dict = {}
    if leads:
        for lead in leads:
            best_sighting = (
                db.query(Sighting)
                .filter(
                    Sighting.requirement_id == requirement_id,
                    Sighting.vendor_name_normalized == lead.vendor_name_normalized,
                )
                .order_by(Sighting.created_at.desc().nullslast())
                .first()
            )
            if best_sighting:
                lead_sighting_data[lead.id] = {
                    "qty_available": best_sighting.qty_available,
                    "unit_price": best_sighting.unit_price,
                }
    return lead_sighting_data


# ── seed helpers ─────────────────────────────────────────────────────────────
def _req(db: Session) -> tuple[Requirement, Requisition]:
    user = User(email="perf7@trioscs.com", name="Perf7", role="buyer")
    db.add(user)
    db.flush()
    requisition = Requisition(name="PERF7-REQ", status="open", created_by=user.id)
    db.add(requisition)
    db.flush()
    requirement = Requirement(
        requisition_id=requisition.id, primary_mpn="LM317T", normalized_mpn="lm317t", target_qty=100
    )
    db.add(requirement)
    db.flush()
    return requirement, requisition


_LEAD_SEQ = [0]


def _lead(db: Session, req: Requirement, requisition: Requisition, norm: str, *, part: str = "LM317T") -> SourcingLead:
    _LEAD_SEQ[0] += 1
    lead = SourcingLead(
        lead_id=f"L-{_LEAD_SEQ[0]}",
        requirement_id=req.id,
        requisition_id=requisition.id,
        part_number_requested="LM317T",
        part_number_matched=part,
        vendor_name=norm.title(),
        vendor_name_normalized=norm,
        primary_source_type="brokerbin",
        primary_source_name="BrokerBin",
    )
    db.add(lead)
    db.flush()
    return lead


def _sighting(db: Session, req: Requirement, norm: str, *, qty, price, created_at) -> Sighting:
    s = Sighting(
        requirement_id=req.id,
        vendor_name=norm.title(),
        vendor_name_normalized=norm,
        qty_available=qty,
        unit_price=price,
        created_at=created_at,
    )
    db.add(s)
    db.flush()
    return s


def _force_null_created_at(db: Session, sighting: Sighting) -> None:
    """Force a genuine NULL created_at (the ORM column default overrides ``=None``)."""
    db.execute(update(Sighting).where(Sighting.id == sighting.id).values(created_at=None))
    db.expire(sighting, ["created_at"])


# ── (a) N+1 guard: query count must not scale with lead count ────────────────
def test_helper_query_count_independent_of_lead_count(db_session: Session):
    req, requisition = _req(db_session)
    _lead(db_session, req, requisition, "vendor-a")
    _sighting(db_session, req, "vendor-a", qty=10, price=Decimal("1.00"), created_at=datetime.now(UTC))
    db_session.commit()

    leads1 = db_session.query(SourcingLead).filter_by(requirement_id=req.id).all()
    with _QueryCounter(db_session) as c1:
        _lead_sighting_data(db_session, req.id, leads1)
    one = c1.count

    now = datetime.now(UTC)
    for i, name in enumerate(("vendor-b", "vendor-c", "vendor-d")):
        _lead(db_session, req, requisition, name)
        _sighting(db_session, req, name, qty=20 + i, price=Decimal("2.00"), created_at=now)
    db_session.commit()

    leads4 = db_session.query(SourcingLead).filter_by(requirement_id=req.id).all()
    assert len(leads4) == 4
    with _QueryCounter(db_session) as c4:
        _lead_sighting_data(db_session, req.id, leads4)
    four = c4.count

    # A per-lead N+1 would make `four` == one+3. The batched helper issues exactly one.
    assert four == one, f"N+1 on sightings: 1 lead={one} queries, 4 leads={four}"
    assert one == 1, f"helper should issue exactly one query, got {one}"


# ── (b) behavioral equivalence across the multi-row edge cases ───────────────
def test_helper_matches_old_loop_across_edge_cases(db_session: Session):
    req, requisition = _req(db_session)
    now = datetime.now(UTC)

    # V1 — three sightings, distinct created_at → latest (qty=7, price=0.70) wins.
    v1 = _lead(db_session, req, requisition, "multi-vendor")
    _sighting(db_session, req, "multi-vendor", qty=5, price=Decimal("0.50"), created_at=now - timedelta(hours=3))
    _sighting(db_session, req, "multi-vendor", qty=6, price=Decimal("0.60"), created_at=now - timedelta(hours=2))
    _sighting(db_session, req, "multi-vendor", qty=7, price=Decimal("0.70"), created_at=now - timedelta(hours=1))

    # V2 — a lead with NO sighting → must be absent from the map.
    v2 = _lead(db_session, req, requisition, "empty-vendor")

    # V3 — single sighting with NULL qty/price → present, with Nones preserved.
    v3 = _lead(db_session, req, requisition, "null-metrics-vendor")
    _sighting(db_session, req, "null-metrics-vendor", qty=None, price=None, created_at=now - timedelta(hours=1))

    # V4 — NULL created_at must sort LAST: the non-null row (qty=7) wins over NULL (qty=999).
    v4 = _lead(db_session, req, requisition, "null-created-vendor")
    s_null = _sighting(db_session, req, "null-created-vendor", qty=999, price=Decimal("9.99"), created_at=now)
    _force_null_created_at(db_session, s_null)
    _sighting(db_session, req, "null-created-vendor", qty=7, price=Decimal("0.07"), created_at=now - timedelta(hours=4))

    # V5 — TWO leads sharing one vendor_name_normalized (distinct part_number_matched) →
    # both lead ids get the SAME sighting data.
    v5a = _lead(db_session, req, requisition, "shared-vendor", part="PART-A")
    v5b = _lead(db_session, req, requisition, "shared-vendor", part="PART-B")
    _sighting(db_session, req, "shared-vendor", qty=42, price=Decimal("4.20"), created_at=now - timedelta(hours=1))

    db_session.commit()

    leads = db_session.query(SourcingLead).filter_by(requirement_id=req.id).order_by(SourcingLead.id).all()

    new = _lead_sighting_data(db_session, req.id, leads)
    old = _old_lead_sighting_data(db_session, req.id, leads)

    # Byte-for-byte identical to the pre-refactor loop.
    assert new == old

    # And the concrete expectations, spelled out (guards the oracle itself):
    assert new[v1.id] == {"qty_available": 7, "unit_price": Decimal("0.70")}
    assert v2.id not in new
    assert new[v3.id] == {"qty_available": None, "unit_price": None}
    assert new[v4.id] == {"qty_available": 7, "unit_price": Decimal("0.07")}
    assert new[v5a.id] == {"qty_available": 42, "unit_price": Decimal("4.20")}
    assert new[v5b.id] == {"qty_available": 42, "unit_price": Decimal("4.20")}


def test_helper_empty_leads_returns_empty(db_session: Session):
    req, _ = _req(db_session)
    db_session.commit()
    assert _lead_sighting_data(db_session, req.id, []) == {}
    assert _lead_sighting_data(db_session, req.id, []) == _old_lead_sighting_data(db_session, req.id, [])


# ── integration: the real endpoint renders sighting data without a 500 ───────
def test_results_partial_renders_sighting_data(client, db_session: Session, test_user: User):
    req, requisition = _req(db_session)
    lead = _lead(db_session, req, requisition, "arrow-electronics")
    _sighting(db_session, req, "arrow-electronics", qty=1234, price=Decimal("0.42"), created_at=datetime.now(UTC))
    db_session.commit()

    resp = client.get(f"/v2/partials/sourcing/{req.id}")
    assert resp.status_code == 200
    # The batched query fed the template the lead's qty — proves the real path works on
    # the DB dialect under test (no PG-only SQL that would 500 in prod / pass on sqlite).
    assert "1,234" in resp.text or "1234" in resp.text
    assert lead.vendor_name in resp.text
