"""test_prospect_buyer_ready_persistence.py — Wave 5 prospecting persistence.

Covers the two deferred prospecting-persistence items:

1. ``prospect_accounts.buyer_ready_score`` is a write-through CACHE of
   ``build_priority_snapshot``'s composite score: it is populated on insert, refreshed
   when scoring inputs change, matches the live recompute, and is read back from the DB.
   It also drives the ``buyer_ready_desc`` SQL ranking (paging in SQL, not in memory).
2. The warm-intro lookup is fully pg_trgm-indexed after the migration: the new
   ``ix_sightings_vendor_email_trgm`` (migration 170) plus the pre-existing
   ``ix_site_contacts_email_trgm`` (migration a513288799de). Postgres-only via the
   ``@requires_postgres`` marker + ``pg_engine`` fixture — runs in the Postgres-paths CI
   job (``PG_TEST_DSN`` set) and is skipped cleanly on the in-memory SQLite suite.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session, client), app.models.prospect_account,
    app.services.prospect_priority
"""

import uuid

from sqlalchemy import text

from app.models.prospect_account import ProspectAccount
from app.services.prospect_priority import build_priority_snapshot
from tests.conftest import requires_postgres


def _make(db, **kw) -> ProspectAccount:
    defaults = dict(
        name=f"Prospect {uuid.uuid4().hex[:6]}",
        domain=f"p-{uuid.uuid4().hex[:8]}.com",
        status="suggested",
        discovery_source="manual",
    )
    defaults.update(kw)
    p = ProspectAccount(**defaults)
    db.add(p)
    db.commit()
    return p


# ── Item 1: persisted buyer_ready_score cache ─────────────────────────────


class TestBuyerReadyScorePersistence:
    def test_persisted_score_matches_recompute_and_is_read_back(self, db_session):
        p = _make(
            db_session,
            fit_score=78,
            readiness_score=64,
            readiness_signals={"intent": {"strength": "strong"}},
            contacts_preview=[{"name": "DM", "verified": True, "seniority": "decision_maker"}],
        )
        expected = build_priority_snapshot(p)["buyer_ready_score"]

        # Read the value straight back from the DB (drop the identity-map copy first).
        pid = p.id
        db_session.expire_all()
        fresh = db_session.get(ProspectAccount, pid)
        assert fresh.buyer_ready_score == expected
        assert fresh.buyer_ready_score >= 70  # this prospect is genuinely buyer-ready

    def test_cache_refreshes_when_scoring_inputs_change(self, db_session):
        p = _make(db_session, fit_score=30, readiness_score=15, readiness_signals={})
        low = p.buyer_ready_score
        assert low == build_priority_snapshot(p)["buyer_ready_score"]

        # Strengthen the inputs and re-commit — the listener must re-cache.
        p.fit_score = 85
        p.readiness_score = 72
        p.readiness_signals = {"intent": {"strength": "strong"}}
        p.contacts_preview = [{"name": "DM", "verified": True, "seniority": "decision_maker"}]
        db_session.commit()

        pid = p.id
        db_session.expire_all()
        fresh = db_session.get(ProspectAccount, pid)
        assert fresh.buyer_ready_score == build_priority_snapshot(fresh)["buyer_ready_score"]
        assert fresh.buyer_ready_score > low

    def test_minimal_prospect_gets_a_non_null_cache_on_insert(self, db_session):
        # A barely-populated row still gets a deterministic, non-null cached score so the
        # SQL sort never has to coalesce a NULL for freshly created prospects.
        p = _make(db_session)
        assert p.buyer_ready_score is not None
        assert p.buyer_ready_score == build_priority_snapshot(p)["buyer_ready_score"]


# ── Item 1 (read path): buyer_ready_desc ranks by the persisted column ────


class TestBuyerReadySortReadsPersistedColumn:
    def test_sort_ranks_by_persisted_score_and_pages_in_sql(self, client, db_session):
        _make(
            db_session,
            name="ZZZ_TopReadyCo",  # name sorts LAST alphabetically — proves score wins
            fit_score=92,
            readiness_score=88,
            readiness_signals={"intent": {"strength": "strong"}, "contacts_verified_count": 3},
            contacts_preview=[{"verified": True, "seniority": "decision_maker", "name": "DM"}],
        )
        _make(db_session, name="AAA_MidReadyCo", fit_score=60, readiness_score=45)
        _make(db_session, name="AAB_LowReadyCo", fit_score=15, readiness_score=8, readiness_signals={})

        resp = client.get("/v2/partials/prospecting?sort=buyer_ready_desc")
        assert resp.status_code == 200
        body = resp.text
        assert body.index("ZZZ_TopReadyCo") < body.index("AAA_MidReadyCo") < body.index("AAB_LowReadyCo")

        # Paging happens in SQL: with per_page=2 the lowest-scored row is off page 1.
        page1 = client.get("/v2/partials/prospecting?sort=buyer_ready_desc&per_page=2&page=1").text
        assert "ZZZ_TopReadyCo" in page1
        assert "AAB_LowReadyCo" not in page1


# ── Item 2: warm-intro pg_trgm GIN indexes (Postgres-only) ────────────────


@requires_postgres
def test_warm_intro_trgm_indexes_exist_after_migration(pg_engine):
    """The warm-intro trgm GIN indexes + the buyer_ready btree index exist on Postgres.

    The ``pg_engine`` fixture creates ``pg_trgm`` then builds the full ORM schema, which
    carries the same indexes migration 170 (``ix_sightings_vendor_email_trgm``,
    ``ix_prospect_accounts_buyer_ready_score``) and migration a513288799de
    (``ix_site_contacts_email_trgm``) create in production — reconciled into the models so
    the drift gate keeps model DDL and migration DDL in lock-step (#464). Both warm-intro
    email indexes must be GIN (trgm) so the leading-wildcard ILIKE scan can use them.
    Runs only in the Postgres-paths CI job (``PG_TEST_DSN`` set); skipped on SQLite.
    """
    with pg_engine.connect() as conn:
        present = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes WHERE indexname IN "
                    "('ix_sightings_vendor_email_trgm','ix_site_contacts_email_trgm',"
                    "'ix_prospect_accounts_buyer_ready_score')"
                )
            )
        }
        gin = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT c.relname FROM pg_index x "
                    "JOIN pg_class c ON c.oid = x.indexrelid "
                    "JOIN pg_am am ON am.oid = c.relam "
                    "WHERE am.amname = 'gin' AND c.relname IN "
                    "('ix_sightings_vendor_email_trgm','ix_site_contacts_email_trgm')"
                )
            )
        }

    assert "ix_sightings_vendor_email_trgm" in present  # new in migration 170
    assert "ix_site_contacts_email_trgm" in present  # pre-existing (a513288799de)
    assert "ix_prospect_accounts_buyer_ready_score" in present
    # Both warm-intro indexes must be GIN (trgm) so the leading-wildcard ILIKE can use them.
    assert gin == {"ix_sightings_vendor_email_trgm", "ix_site_contacts_email_trgm"}
