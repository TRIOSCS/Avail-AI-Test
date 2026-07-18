"""tests/test_search_service_requirementless.py — Requirement-less Sighting persistence
for interactive/global "quick search" results.

Covers:
- app.search_service._save_sightings(req=None) — requirement-less write path,
  (vendor, mpn) dedup, and which requirement-scoped steps are correctly skipped.
- app.search_service._persist_interactive_sightings — the write-session worker.
- app.search_service.stream_search_mpn — a LIVE (non-cache-hit) run persists;
  a cache-HIT run does NOT re-persist.

Called by: pytest
Depends on: app/search_service.py, app/models/sourcing.py (Sighting.requirement_id
            nullable, migration 196_sighting_req_id_nullable)
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialVendorHistory, Sighting, VendorCard, VendorContact
from app.models.sourcing_lead import SourcingLead
from app.models.vendor_sighting_summary import VendorSightingSummary
from app.search_service import _persist_interactive_sightings, _save_sightings, stream_search_mpn
from app.vendor_utils import normalize_vendor_name

# ── Helpers ──────────────────────────────────────────────────────────────


def _fresh_hit(vendor_name: str, mpn: str = "LM317T", **overrides) -> dict:
    hit = {
        "vendor_name": vendor_name,
        "mpn_matched": mpn,
        "qty_available": 100,
        "unit_price": 1.0,
        "currency": "USD",
        "source_type": "nexar",
        "is_authorized": False,
        "confidence": 3,
        "vendor_email": "sales@arrow-test.com",
    }
    hit.update(overrides)
    return hit


# ── _save_sightings(req=None) ─────────────────────────────────────────────


class TestSaveSightingsRequirementLess:
    def test_creates_sightings_with_null_requirement_id(self, db_session: Session):
        fresh = [_fresh_hit("Arrow Electronics")]
        result = _save_sightings(fresh, None, db_session, succeeded_sources={"nexar"})

        assert len(result) == 1
        assert result[0].requirement_id is None
        assert result[0].vendor_name == "Arrow Electronics"
        row = db_session.query(Sighting).filter_by(vendor_name="Arrow Electronics").one()
        assert row.requirement_id is None

    def test_dedup_replaces_stale_row_by_vendor_and_mpn(self, db_session: Session):
        """A prior requirement-less (vendor, mpn) row is deleted and replaced by the
        fresh one — mirrors the requirement-scoped 'keep fresh' merge policy."""
        stale = Sighting(
            requirement_id=None,
            vendor_name="Arrow Electronics",
            vendor_name_normalized=normalize_vendor_name("Arrow Electronics"),
            mpn_matched="LM317T",
            normalized_mpn="lm317t",
            source_type="nexar",
            unit_price=0.10,
            qty_available=1,
            created_at=datetime.now(UTC),
        )
        db_session.add(stale)
        db_session.commit()
        stale_id = stale.id
        # SQLite can reassign a deleted row's PK to the next insert, so an
        # id-based "was it replaced" check would be flaky — expunge instead and
        # assert on content (price/qty) plus the direct stale-id lookup below.
        db_session.expunge(stale)

        fresh = [_fresh_hit("Arrow Electronics", unit_price=2.50, qty_available=999)]
        result = _save_sightings(fresh, None, db_session, succeeded_sources={"nexar"})

        assert len(result) == 1
        remaining = db_session.query(Sighting).filter_by(vendor_name="Arrow Electronics").all()
        assert len(remaining) == 1
        assert remaining[0].unit_price == 2.50
        assert remaining[0].qty_available == 999

    def test_dedup_preserves_different_mpn_for_same_vendor(self, db_session: Session):
        """A pre-existing requirement-less row for a DIFFERENT mpn from the same vendor
        must survive — dedup is keyed on (vendor, mpn), not vendor alone."""
        other_mpn = Sighting(
            requirement_id=None,
            vendor_name="Arrow Electronics",
            vendor_name_normalized=normalize_vendor_name("Arrow Electronics"),
            mpn_matched="NE555P",
            normalized_mpn="ne555p",
            source_type="nexar",
            created_at=datetime.now(UTC),
        )
        db_session.add(other_mpn)
        db_session.commit()
        other_id = other_mpn.id

        fresh = [_fresh_hit("Arrow Electronics", mpn="LM317T")]
        _save_sightings(fresh, None, db_session, succeeded_sources={"nexar"})

        survivor = db_session.get(Sighting, other_id)
        assert survivor is not None
        assert survivor.normalized_mpn == "ne555p"
        assert survivor.requirement_id is None
        arrow_mpns = {
            s.normalized_mpn for s in db_session.query(Sighting).filter_by(vendor_name="Arrow Electronics").all()
        }
        assert arrow_mpns == {"ne555p", "lm317t"}

    def test_lead_sync_and_vendor_summary_skipped_no_error(self, db_session: Session):
        """Requirement-scoped steps (SourcingLead sync, VendorSightingSummary rebuild)
        must not run — their FKs are NOT NULL and there is no requirement — and the call
        must not raise."""
        fresh = [_fresh_hit("Arrow Electronics")]
        result = _save_sightings(fresh, None, db_session, succeeded_sources={"nexar"})

        assert len(result) == 1
        assert db_session.query(SourcingLead).count() == 0
        assert db_session.query(VendorSightingSummary).count() == 0

    def test_vendor_card_email_and_material_history_still_created(self, db_session: Session):
        """Non-requirement-scoped steps keep running: vendor email propagation and
        (via the caller's material-card upsert) MaterialVendorHistory."""
        vc = VendorCard(
            normalized_name=normalize_vendor_name("Arrow Electronics"),
            display_name="Arrow Electronics",
            vendor_score=80.0,
            created_at=datetime.now(UTC),
        )
        db_session.add(vc)
        db_session.commit()

        fresh = [_fresh_hit("Arrow Electronics")]
        sightings = _save_sightings(fresh, None, db_session, succeeded_sources={"nexar"})
        assert len(sightings) == 1

        contact = db_session.query(VendorContact).filter_by(vendor_card_id=vc.id).one()
        assert contact.email == "sales@arrow-test.com"


# ── _persist_interactive_sightings ────────────────────────────────────────


class TestPersistInteractiveSightings:
    def test_empty_hits_returns_none_and_writes_nothing(self, db_session: Session):
        result = _persist_interactive_sightings("LM317T", [], set(), datetime.now(UTC), db_session.get_bind())
        assert result is None
        assert db_session.query(Sighting).count() == 0

    def test_persists_sighting_and_material_card(self, db_session: Session):
        raw_hits = [_fresh_hit("Arrow Electronics")]
        now = datetime.now(UTC)
        result = _persist_interactive_sightings("LM317T", raw_hits, {"nexar"}, now, db_session.get_bind())

        assert result is not None
        assert result["sighting_count"] == 1
        assert len(result["card_ids"]) == 1

        rows = db_session.query(Sighting).all()
        assert len(rows) == 1
        assert rows[0].requirement_id is None
        assert rows[0].vendor_name == "Arrow Electronics"

        card = db_session.get(MaterialCard, result["card_ids"][0])
        assert card is not None
        assert card.normalized_mpn == "lm317t"
        assert card.last_searched_at is not None
        vh = db_session.query(MaterialVendorHistory).filter_by(material_card_id=card.id).all()
        assert len(vh) == 1
        assert vh[0].vendor_name_normalized == normalize_vendor_name("Arrow Electronics")


# ── stream_search_mpn wiring ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _own_session_and_engine(db_session):
    """stream_search_mpn opens its own SessionLocal() for reads and its own
    sessionmaker(bind=engine) (via _persist_interactive_sightings) for the post-"done"
    persistence write — point BOTH at the test session/engine so the worker never
    touches the real (schema-less under TESTING) database.

    Uses ``db_session.get_bind()`` rather than importing ``tests.conftest.engine``
    directly: ``tests`` has no ``__init__.py`` (namespace package), so pytest's own
    conftest collection and an explicit ``from tests.conftest import engine`` in a
    test module resolve to two DISTINCT ``sys.modules`` entries ("conftest" vs
    "tests.conftest"), each re-executing the module top level and creating its OWN
    separate in-memory engine — writes via the import-based ``engine`` would
    silently land in a different database than ``db_session`` reads from.
    """
    with (
        patch("app.search_service.SessionLocal", lambda: db_session),
        patch("app.search_service.engine", db_session.get_bind()),
    ):
        yield


class TestStreamSearchMpnPersistsRequirementLessSightings:
    async def test_live_fetch_persists_sightings_after_done(self, db_session: Session):
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.__class__.__name__ = "NexarConnector"
        mock_conn.source_name = "nexar"
        mock_conn.search = AsyncMock(
            return_value=[
                {
                    "vendor_name": "Arrow Electronics",
                    "mpn_matched": "LM317T",
                    "qty_available": 500,
                    "unit_price": 0.45,
                    "source_type": "nexar",
                    "is_authorized": True,
                }
            ]
        )

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch("app.search_service._get_search_cache", return_value=None),
            patch("app.search_service._set_search_cache"),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
        ):
            await stream_search_mpn("live-persist-search", "LM317T")

        event_types = [c[0][1] for c in mock_broker.publish.call_args_list]
        assert "done" in event_types

        rows = db_session.query(Sighting).filter_by(vendor_name="Arrow Electronics").all()
        assert len(rows) == 1
        assert rows[0].requirement_id is None
        assert rows[0].mpn_matched == "LM317T"
        assert float(rows[0].unit_price) == 0.45

    async def test_cache_hit_does_not_persist(self, db_session: Session):
        mock_broker = MagicMock()
        mock_broker.publish = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.__class__.__name__ = "NexarConnector"
        mock_conn.source_name = "nexar"
        mock_conn.search = AsyncMock(return_value=[])

        cached_results = [
            {
                "vendor_name": "CachedOnlyVendor",
                "mpn_matched": "LM317T",
                "qty_available": 10,
                "unit_price": 1.0,
                "source_type": "nexar",
                "confidence": 3,
            }
        ]
        cached_stats = [{"source": "nexar", "results": 1, "ms": 50, "error": None, "status": "ok"}]

        with (
            patch("app.search_service._build_connectors", return_value=([mock_conn], {}, set())),
            patch("app.services.sse_broker.broker", mock_broker),
            patch(
                "app.search_service._get_search_cache",
                return_value=(cached_results, cached_stats, "2026-01-01T00:00:00+00:00"),
            ),
            patch("app.search_service._render_search_vendor_cards_html", return_value="<div></div>"),
        ):
            await stream_search_mpn("cache-hit-no-persist", "LM317T")

        event_types = [c[0][1] for c in mock_broker.publish.call_args_list]
        assert "done" in event_types
        assert db_session.query(Sighting).filter_by(vendor_name="CachedOnlyVendor").count() == 0
