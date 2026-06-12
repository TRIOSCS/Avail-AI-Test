"""Backfill CLI: select_candidates demand-first ordering, the shared
pending_resolution freshness selector (90-day negative cache, frozen clock), AND the
run() resolve loop itself — a parallel implementation of the worker Pass A contract,
so its budget discipline is pinned independently: stop-at-either-cap with per-
iteration counter re-reads (boundary, not just exhaustion), bill-before-await on the
ClaudeError path, the 5-consecutive-ClaudeError abort + reset-on-success, per-item
pending re-check (concurrent-worker skip), IntegrityError-at-commit tolerance,
--limit slicing and --dry-run making zero web calls. resolve_oem_spare and the
counter cache are mocked — no web call is ever made from this file.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy.orm import Session

from app.constants import OemCrosswalkStatus
from app.management.backfill_oem_crosswalk import run, select_candidates
from app.models import MaterialCard, OemCrosswalk
from app.services.enrichment_worker.config import EnrichmentWorkerConfig
from app.services.enrichment_worker.oem_crosswalk_resolver import OemResolveResult
from app.services.oem_crosswalk_enrich import NO_MATCH_RETRY_DAYS, pending_resolution
from app.utils.claude_errors import ClaudeError

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)

RESOLVED = OemResolveResult(
    status="resolved",
    canonical_mpn="ST4000NM0035",
    manufacturer="Seagate",
    source_url="https://partsurfer.hp.com/Search.aspx?SearchText=695510-001",
    source_domain="partsurfer.hp.com",
    confidence=0.95,
    payload={"canonical_mpn": "ST4000NM0035"},
)
NO_MATCH = OemResolveResult(status="no_match", payload={"canonical_mpn": None})


class _NoCloseSession:
    """Proxy handing the test session to run() while neutering its finally-close."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        return getattr(self._db, name)


def _run_cli(
    db_session,
    resolve_mock,
    *,
    limit=None,
    dry_run=False,
    cache_counts: dict | None = None,
    cfg: EnrichmentWorkerConfig | None = None,
):
    """Drive run() with the resolver, counter cache, config, session and sleep mocked.

    Returns (attempted, counters) — counters is the simulated shared store the atomic
    incr advances (what Redis INCRBY provides in prod).
    """
    cfg = cfg or EnrichmentWorkerConfig(web_daily_cap=80, oem_resolve_daily_cap=40)
    counters = dict(cache_counts or {})

    def _fragment(key):
        for fragment in ("web_calls", "oem_resolves"):
            if fragment in key:
                return fragment
        return key

    def get_count(key):
        return counters.get(_fragment(key), 0)

    def incr_count(key, amount=1, ttl_days=1.0):
        frag = _fragment(key)
        counters[frag] = counters.get(frag, 0) + amount
        return counters[frag]

    with (
        patch("app.database.SessionLocal", return_value=_NoCloseSession(db_session)),
        patch("app.management.backfill_oem_crosswalk.EnrichmentWorkerConfig.from_env", return_value=cfg),
        patch("app.management.backfill_oem_crosswalk.resolve_oem_spare", resolve_mock),
        patch("app.management.backfill_oem_crosswalk.intel_cache.get_count", side_effect=get_count),
        patch("app.management.backfill_oem_crosswalk.intel_cache.incr_count", side_effect=incr_count),
        patch("app.management.backfill_oem_crosswalk.asyncio.sleep", new=AsyncMock()),
    ):
        attempted = asyncio.run(run("hpe", limit, dry_run))
    return attempted, counters


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
    resolved = status == OemCrosswalkStatus.RESOLVED
    row = OemCrosswalk(
        spare_raw=norm,
        spare_norm=norm,
        vendor=vendor,
        status=status,
        # ck_oem_crosswalk_status_canonical: resolved rows MUST carry a canonical.
        canonical_mpn_raw="ST4000NM0035" if resolved else None,
        canonical_mpn_norm="st4000nm0035" if resolved else None,
        source_domain="partsurfer.hp.com" if resolved else "",
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


def _hpe_card(db: Session, mpn: str, search_count: int = 1) -> MaterialCard:
    return _card(db, mpn, category="cpu", search_count=search_count)


def test_run_resolves_pending_and_commits_per_row(db_session: Session):
    # Happy path: both queued spares resolve; rows are upserted with per-row commits
    # (progress survives interruption) and both counters advance once per resolve.
    _hpe_card(db_session, "875940-001")
    _hpe_card(db_session, "875941-001")
    resolve_mock = AsyncMock(side_effect=[RESOLVED, NO_MATCH])

    attempted, counters = _run_cli(db_session, resolve_mock)

    assert attempted == 2
    rows = db_session.query(OemCrosswalk).all()
    assert {r.status for r in rows} == {OemCrosswalkStatus.RESOLVED, OemCrosswalkStatus.NO_MATCH}
    assert counters == {"web_calls": 2, "oem_resolves": 2}


def test_run_dry_run_makes_zero_web_calls_and_writes_nothing(db_session: Session):
    _hpe_card(db_session, "875940-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    attempted, counters = _run_cli(db_session, resolve_mock, dry_run=True)

    assert attempted == 0
    resolve_mock.assert_not_awaited()
    assert db_session.query(OemCrosswalk).count() == 0
    assert counters == {}


def test_run_limit_slices_after_pending_filter(db_session: Session):
    # Three pending spares, --limit 1 → exactly one resolve.
    for i in range(3):
        _hpe_card(db_session, f"87594{i}-001")
    resolve_mock = AsyncMock(return_value=RESOLVED)

    attempted, _ = _run_cli(db_session, resolve_mock, limit=1)

    assert attempted == 1
    assert resolve_mock.await_count == 1


def test_run_stops_at_web_cap_boundary_mid_loop(db_session: Session):
    # 79/80 with three queued → exactly ONE resolve then stop (the boundary, not just
    # exhaustion — an off-by-one or a reserve above the check would leak or strand).
    for i in range(3):
        _hpe_card(db_session, f"87594{i}-001")
    resolve_mock = AsyncMock(return_value=NO_MATCH)

    attempted, counters = _run_cli(db_session, resolve_mock, cache_counts={"web_calls": 79})

    assert attempted == 1
    assert resolve_mock.await_count == 1
    assert counters["web_calls"] == 80


def test_run_stops_at_oem_sub_cap_boundary_mid_loop(db_session: Session):
    # Mirror boundary for the sub-cap: 39/40 with three queued → exactly one resolve.
    for i in range(3):
        _hpe_card(db_session, f"87594{i}-001")
    resolve_mock = AsyncMock(return_value=NO_MATCH)

    attempted, counters = _run_cli(db_session, resolve_mock, cache_counts={"oem_resolves": 39})

    assert attempted == 1
    assert counters["oem_resolves"] == 40


def test_run_aborts_after_five_consecutive_claude_errors_still_bills(db_session: Session):
    # A backend outage must not burn the day's budget: exactly 5 awaits then abort —
    # and every dispatched call is billed (bill-before-await) despite all failing.
    for i in range(8):
        _hpe_card(db_session, f"87594{i}-001")
    resolve_mock = AsyncMock(side_effect=ClaudeError("boom"))

    attempted, counters = _run_cli(db_session, resolve_mock)

    assert attempted == 5
    assert resolve_mock.await_count == 5
    assert db_session.query(OemCrosswalk).count() == 0  # transient failures write NO row
    assert counters == {"web_calls": 5, "oem_resolves": 5}


def test_run_success_resets_consecutive_error_counter(db_session: Session):
    # 4 errors, a success, then 5 more errors: the success must reset the counter, so
    # the loop survives past the 5th overall error and aborts on the 5th CONSECUTIVE.
    for i in range(12):
        _hpe_card(db_session, f"8759{i:02d}-001")
    effects = [ClaudeError("e")] * 4 + [NO_MATCH] + [ClaudeError("e")] * 5 + [RESOLVED]
    resolve_mock = AsyncMock(side_effect=effects)

    attempted, _ = _run_cli(db_session, resolve_mock)

    assert resolve_mock.await_count == 10  # 4 errors + 1 success + 5 errors → abort
    assert attempted == 10
    assert db_session.query(OemCrosswalk).count() == 1  # only the success wrote a row


def test_run_recheck_skips_norm_cached_by_concurrent_worker(db_session: Session):
    # The startup pending snapshot goes stale mid-run: while resolving the FIRST norm,
    # the live worker caches the SECOND. The per-item re-check must skip it — no
    # second billed resolve, no duplicate row.
    a = _hpe_card(db_session, "875940-001")
    b = _hpe_card(db_session, "875941-001")
    assert a and b

    async def racing_resolve(display, norm, vendor):
        if norm == "875940001":
            db_session.add(
                OemCrosswalk(
                    spare_raw="875941-001",
                    spare_norm="875941001",
                    vendor="hpe",
                    status=OemCrosswalkStatus.NO_MATCH,
                    looked_up_at=datetime.now(timezone.utc),
                )
            )
            db_session.flush()
        return NO_MATCH

    resolve_mock = AsyncMock(side_effect=racing_resolve)

    attempted, counters = _run_cli(db_session, resolve_mock)

    assert attempted == 1  # the second norm was skipped before billing
    assert resolve_mock.await_count == 1
    assert db_session.query(OemCrosswalk).filter_by(spare_norm="875941001").count() == 1
    assert counters == {"web_calls": 1, "oem_resolves": 1}


def test_run_integrity_error_at_commit_rolls_back_and_continues(db_session: Session):
    # The worker commits the same edge DURING the CLI's await (after the re-check):
    # the per-row commit raises IntegrityError — the CLI must rollback and continue
    # the drain, not crash mid-run; the next item still processes.
    from sqlalchemy import text

    _hpe_card(db_session, "875940-001")
    _hpe_card(db_session, "875941-001")

    async def racing_resolve(display, norm, vendor):
        if norm == "875940001":
            # Same edge (spare_norm, vendor, source_domain) the CLI is about to write.
            db_session.execute(
                text(
                    "INSERT INTO oem_crosswalk (spare_raw, spare_norm, vendor, status, canonical_mpn_raw, "
                    "canonical_mpn_norm, source_domain, looked_up_at) VALUES ('875940-001', '875940001', 'hpe', "
                    "'resolved', 'ST4000NM0035', 'st4000nm0035', 'partsurfer.hp.com', '2026-06-10')"
                )
            )
        return RESOLVED

    resolve_mock = AsyncMock(side_effect=racing_resolve)

    attempted, _ = _run_cli(db_session, resolve_mock)

    assert attempted == 2  # the collision did not abort the run
    assert resolve_mock.await_count == 2
    # In this single-session simulation the rollback discards the racing row too (in
    # prod the worker committed it from its own session, so it survives); the pinned
    # contracts are: no duplicate edge, and the NEXT item still committed.
    assert db_session.query(OemCrosswalk).filter_by(spare_norm="875940001").count() == 0
    assert db_session.query(OemCrosswalk).filter_by(spare_norm="875941001").count() == 1
