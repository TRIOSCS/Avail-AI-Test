"""Tests for the per-normalized-MPN 48h cooldown helper used by search_requirement.

Called by: pytest
Depends on: app.search_service._mpn_cooldown_partition, MaterialCard
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.models import MaterialCard, Requirement, Requisition
from app.search_service import _mpn_cooldown_partition, search_requirement
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


class TestSearchRequirementCooldown:
    """Integration tests: search_requirement honors per-MPN cooldown and stamps MaterialCard."""

    async def test_only_stale_mpns_hit_connectors(self, db_session: Session, test_user):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-1",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()

        item = Requirement(
            requisition_id=req.id,
            primary_mpn="STALEMPN",
            substitutes=[{"mpn": "FRESHMPN"}],
            created_at=now,
        )
        db_session.add(item)
        db_session.flush()

        # FRESHMPN already searched 12h ago → should be skipped
        _mk_card(db_session, "FRESHMPN", now - timedelta(hours=12))
        # STALEMPN has no card → should be searched
        db_session.commit()

        with patch(
            "app.search_service._fetch_fresh",
            new=AsyncMock(return_value=([], [])),
        ) as fetch_mock:
            result = await search_requirement(item, db_session)

        # _fetch_fresh called with exactly ["STALEMPN"] (FRESHMPN excluded)
        assert fetch_mock.call_count == 1
        called_pns = fetch_mock.call_args[0][0]
        assert called_pns == ["STALEMPN"]

        # Returned per-MPN map reflects partition
        assert result["mpn_results"] == {
            "STALEMPN": "searched",
            "FRESHMPN": "cached",
        }

    async def test_searched_mpn_card_last_searched_at_updates(self, db_session: Session, test_user):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-2",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="NEWMPN",
            created_at=now,
        )
        db_session.add(item)
        db_session.commit()

        with patch(
            "app.search_service._fetch_fresh",
            new=AsyncMock(return_value=([], [])),
        ):
            await search_requirement(item, db_session)

        card = db_session.query(MaterialCard).filter_by(normalized_mpn=normalize_mpn_key("NEWMPN")).first()
        assert card is not None
        assert card.last_searched_at is not None
        last = card.last_searched_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        assert (now - last).total_seconds() < 60

    async def test_all_cached_path_returns_affinity_matches(self, db_session: Session, test_user):
        """When every MPN is within cooldown, affinity matches still surface."""
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-AFFINITY",
            customer_name="Test Co",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="CACHEDMPN",
            created_at=now,
        )
        db_session.add(item)
        _mk_card(db_session, "CACHEDMPN", now - timedelta(hours=1))
        db_session.commit()

        # Mock both _fetch_fresh (should NOT be called) and find_vendor_affinity
        # (should be called even on the all-cached short-circuit path)
        fake_match = {"vendor_name": "FakeVendor", "score": 80}
        with (
            patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))) as fetch_mock,
            patch(
                "app.search_service.find_vendor_affinity",
                return_value=[fake_match],
            ) as affinity_mock,
        ):
            result = await search_requirement(item, db_session)

        fetch_mock.assert_not_called()
        affinity_mock.assert_called_once()
        # Affinity match surfaces in sightings list even though no connectors fired
        vendor_names = {s.get("vendor_name") for s in result.get("sightings", [])}
        assert "FakeVendor" in vendor_names
        # mpn_results still reflects the cached state
        assert result["mpn_results"] == {"CACHEDMPN": "cached"}


class TestIcsNcEnqueueOnRefresh:
    async def test_enqueues_ics_and_nc_once_per_call(self, db_session: Session, test_user):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-3",
            customer_name="C",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="EM1",
            substitutes=[{"mpn": "EM2"}],
            created_at=now,
        )
        db_session.add(item)
        db_session.commit()

        with (
            patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))),
            patch("app.search_service.enqueue_for_ics_search") as ics_mock,
            patch("app.search_service.enqueue_for_nc_search") as nc_mock,
        ):
            await search_requirement(item, db_session)

        assert ics_mock.call_count == 1
        assert nc_mock.call_count == 1
        # Called with (requirement_id, db_session)
        for m in (ics_mock, nc_mock):
            requirement_ids = [c.args[0] for c in m.call_args_list]
            assert requirement_ids == [item.id]

    async def test_no_enqueue_on_all_cached_short_circuit(self, db_session: Session, test_user):
        now = datetime.now(timezone.utc)
        req = Requisition(
            name="REQ-CD-CACHED",
            customer_name="C",
            status="active",
            created_by=test_user.id,
            created_at=now,
        )
        db_session.add(req)
        db_session.flush()
        item = Requirement(
            requisition_id=req.id,
            primary_mpn="CACHED1",
            created_at=now,
        )
        db_session.add(item)
        _mk_card(db_session, "CACHED1", now - timedelta(hours=1))
        db_session.commit()

        with (
            patch("app.search_service._fetch_fresh", new=AsyncMock(return_value=([], []))),
            patch("app.search_service.enqueue_for_ics_search") as ics_mock,
            patch("app.search_service.enqueue_for_nc_search") as nc_mock,
        ):
            await search_requirement(item, db_session)

        ics_mock.assert_not_called()
        nc_mock.assert_not_called()
