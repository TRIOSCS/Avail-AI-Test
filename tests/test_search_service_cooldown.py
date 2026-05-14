"""Tests for the per-normalized-MPN 48h cooldown helper used by search_requirement.

Called by: pytest
Depends on: app.search_service._mpn_cooldown_partition, MaterialCard
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.search_service import _mpn_cooldown_partition
from app.utils.normalization import normalize_mpn_key


def _mk_card(db: Session, mpn: str, last_searched_at):
    # MaterialCard.normalized_mpn is the canonical key produced by
    # normalize_mpn_key (lowercase, alphanumeric only); display_mpn is the
    # NOT NULL surface form. Use the helper to stay consistent with how
    # production code populates these columns.
    card = MaterialCard(
        normalized_mpn=normalize_mpn_key(mpn),
        display_mpn=mpn,
        last_searched_at=last_searched_at,
    )
    db.add(card)
    db.flush()
    return card


class TestMpnCooldownPartition:
    def test_partitions_stale_and_fresh_mpns(self, db_session: Session):
        now = datetime.now(timezone.utc)
        fresh_dt = now - timedelta(hours=12)
        stale_dt = now - timedelta(hours=72)
        _mk_card(db_session, "FRESHMPN", fresh_dt)
        _mk_card(db_session, "STALEMPN", stale_dt)
        db_session.commit()

        to_search, cached_ids = _mpn_cooldown_partition(db_session, ["FRESHMPN", "STALEMPN", "NEWMPN"], now=now)

        # STALEMPN (>=48h) and NEWMPN (no card) get searched
        assert set(to_search) == {"STALEMPN", "NEWMPN"}
        # FRESHMPN keeps its card.id in cached_ids so detail panel can still
        # surface those sightings
        cached_card = db_session.query(MaterialCard).filter_by(normalized_mpn=normalize_mpn_key("FRESHMPN")).first()
        assert cached_ids == [cached_card.id]

    def test_null_last_searched_at_is_treated_as_never_searched(self, db_session: Session):
        now = datetime.now(timezone.utc)
        _mk_card(db_session, "NULLMPN", None)
        db_session.commit()

        to_search, cached_ids = _mpn_cooldown_partition(db_session, ["NULLMPN"], now=now)

        assert to_search == ["NULLMPN"]
        assert cached_ids == []

    def test_exactly_48h_boundary_is_searched(self, db_session: Session):
        now = datetime.now(timezone.utc)
        # exactly 48h ago — should be searched (>= 48h)
        _mk_card(db_session, "BOUNDARYMPN", now - timedelta(hours=48))
        db_session.commit()

        to_search, cached_ids = _mpn_cooldown_partition(db_session, ["BOUNDARYMPN"], now=now)

        assert to_search == ["BOUNDARYMPN"]
