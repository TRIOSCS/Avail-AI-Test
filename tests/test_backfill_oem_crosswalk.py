"""Backfill CLI pure-DB pieces: select_candidates demand-first ordering and the
shared pending_resolution freshness selector (90-day negative cache, frozen clock).

The resolve loop itself is the worker Pass A contract (same resolver, same two
counters) — covered in tests/test_oem_crosswalk_worker.py and the resolver tests;
no web call is ever made from this file.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.constants import OemCrosswalkStatus
from app.management.backfill_oem_crosswalk import select_candidates
from app.models import MaterialCard, OemCrosswalk
from app.services.oem_crosswalk_enrich import NO_MATCH_RETRY_DAYS, pending_resolution

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _card(db: Session, mpn: str, category: str | None = None, search_count: int = 0, **kw) -> MaterialCard:
    card = MaterialCard(
        normalized_mpn=mpn.lower(),
        display_mpn=mpn,
        category=category,
        search_count=search_count,
        **kw,
    )
    db.add(card)
    db.flush()
    return card


def _row(db: Session, norm: str, status: str, looked_up_at: datetime, vendor: str = "hpe") -> OemCrosswalk:
    row = OemCrosswalk(
        spare_raw=norm,
        spare_norm=norm,
        vendor=vendor,
        status=status,
        looked_up_at=looked_up_at,
    )
    db.add(row)
    db.flush()
    return row


def test_select_candidates_demand_first_ordering(db_session: Session):
    # Bucket order: (1) cpu + searched, (2) cpu unsearched, (3) other commodities;
    # search_count DESC within a bucket. Non-vendor and deleted cards are excluded.
    _card(db_session, "111111-001", category="hdd", search_count=99)  # bucket 3
    _card(db_session, "222222-001", category="cpu", search_count=0)  # bucket 2
    _card(db_session, "333333-001", category="cpu", search_count=2)  # bucket 1
    _card(db_session, "444444-001", category="cpu", search_count=7)  # bucket 1, more demand
    _card(db_session, "01HW917", category="cpu", search_count=50)  # lenovo — not hpe
    _card(db_session, "555555-001", category="cpu", search_count=9, deleted_at=NOW)  # soft-deleted

    ordered = select_candidates(db_session, "hpe")

    assert [norm for norm, _ in ordered] == ["444444001", "333333001", "222222001", "111111001"]


def test_select_candidates_dedupes_norms_keeping_best_bucket(db_session: Session):
    # Two cards sharing a spare norm (display variants) collapse to ONE candidate in
    # the best (lowest) bucket.
    _card(db_session, "666666-001", category=None, search_count=0)  # bucket 3
    card2 = MaterialCard(normalized_mpn="666666-001x", display_mpn="666666-001 ", category="cpu", search_count=3)
    db_session.add(card2)
    db_session.flush()

    ordered = select_candidates(db_session, "hpe")

    assert len(ordered) == 1
    assert ordered[0][0] == "666666001"


def test_pending_resolution_freshness_windows(db_session: Session):
    # resolved → permanently fresh; no_match inside 90d → blocked; stale no_match →
    # pending WITH the row (updated in place); never-seen → pending with None.
    _row(db_session, "aaa111", OemCrosswalkStatus.RESOLVED, NOW - timedelta(days=400))
    _row(db_session, "bbb222", OemCrosswalkStatus.NO_MATCH, NOW - timedelta(days=NO_MATCH_RETRY_DAYS - 1))
    stale = _row(db_session, "ccc333", OemCrosswalkStatus.NO_MATCH, NOW - timedelta(days=NO_MATCH_RETRY_DAYS + 1))

    pending = pending_resolution(db_session, ["aaa111", "bbb222", "ccc333", "ddd444"], "hpe", now=NOW)

    assert "aaa111" not in pending  # resolved = permanent
    assert "bbb222" not in pending  # fresh negative cache
    assert pending["ccc333"] is stale  # stale negative cache — upsert target
    assert pending["ddd444"] is None  # never looked up — insert


def test_pending_resolution_is_vendor_scoped(db_session: Session):
    # A lenovo row must not satisfy an hpe lookup for the same norm.
    _row(db_session, "eee555", OemCrosswalkStatus.RESOLVED, NOW, vendor="lenovo")

    pending = pending_resolution(db_session, ["eee555"], "hpe", now=NOW)

    assert pending == {"eee555": None}
