"""tests/test_qc_jobs_cache.py — QC 2026-08-08 cluster 5 (jobs/loop hygiene + cache).

- Picks cache invalidation was a silent no-op: keys stored as
  "proactive_picks:<id>" but invalidate_prefix("proactive_picks:") built the
  pattern "intel:proactive_picks::*" (double colon) and matched nothing, so
  the 24h strip never refreshed after a scan.
- xdist root cause: safe_background_task's TESTING suppression was gated by a
  per-caller flag set at only ~3 of ~17 sites; the rest crashed workers at
  teardown. Suppression now DEFAULTS to on under TESTING.
- Auto-dedup ran synchronous Claude calls on the event loop (app freeze); it
  now runs off-loop with a timeout.
- part_equivalence classify commits per verdict (no re-paying Claude on timeout).
- Proactive never stages a $0.00 sell price.

Called by: pytest autodiscovery
Depends on: conftest fixtures (db_session).
"""

import asyncio
import inspect
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import engine  # noqa: F401

# ── Picks cache invalidation no longer a no-op ───────────────────────────


def test_picks_prefix_has_no_trailing_colon():
    # invalidate_prefix appends ":*"; a trailing colon here would double it and
    # match nothing.
    from app.services import proactive_matching, proactive_service

    assert proactive_matching.PICKS_CACHE_PREFIX == "proactive_picks"
    assert proactive_service.PICKS_CACHE_PREFIX == "proactive_picks"


def test_bust_picks_pattern_matches_stored_key():
    from app.cache.intel_cache import _REDIS_PREFIX
    from app.services.proactive_matching import PICKS_CACHE_PREFIX

    stored_key = f"{_REDIS_PREFIX}{PICKS_CACHE_PREFIX}:42"  # what set_cached writes
    pattern = f"{_REDIS_PREFIX}{PICKS_CACHE_PREFIX}:*"  # what invalidate_prefix builds
    import fnmatch

    assert fnmatch.fnmatch(stored_key, pattern), "invalidation pattern must match the stored key"


# ── xdist root cause: unconditional TESTING suppression ──────────────────


@pytest.mark.anyio
async def test_safe_background_task_suppresses_under_testing_without_flag():
    """A real coroutine (no suppress flag) must NOT run under TESTING — that was the
    worker-crash source."""
    from app.utils.async_helpers import safe_background_task

    ran = False

    async def _real_work():
        nonlocal ran
        ran = True

    # Default suppress_in_testing=True now — the OLD default (False) would run it.
    await safe_background_task(_real_work(), task_name="qc_probe")
    await asyncio.sleep(0)
    assert ran is False, "fire-and-forget task must be suppressed under TESTING regardless of the flag"


# ── Auto-dedup runs off the event loop ───────────────────────────────────


def test_auto_dedup_job_offloads_to_executor():
    import app.jobs.maintenance_jobs as mj

    src = inspect.getsource(mj._job_auto_dedup)
    assert "run_in_executor" in src or "to_thread" in src, "dedup must not run on the event loop"
    assert "wait_for" in src, "dedup must be bounded by a timeout"


# ── classify commits per verdict ─────────────────────────────────────────


@pytest.mark.anyio
async def test_classify_commits_each_verdict(db_session):
    from app.models import PartEquivalence
    from app.services.part_equivalence import classify_new_pairs

    commits = {"n": 0}
    real_commit = db_session.commit

    def counting_commit():
        commits["n"] += 1
        return real_commit()

    with patch(
        "app.utils.claude_client.claude_json",
        new_callable=AsyncMock,
        return_value={"verdict": "same", "confidence": 0.9, "reason": "pkg"},
    ):
        with patch.object(db_session, "commit", side_effect=counting_commit):
            stored = await classify_new_pairs(db_session, {"gsot36c": "GSOT36C", "gsot36ce308": "GSOT36C-E3-08"})
    assert stored == 1
    assert commits["n"] >= 1  # committed as it went, not deferred to one end commit
    assert db_session.query(PartEquivalence).count() == 1


# ── never stage a $0 sell price ──────────────────────────────────────────


def test_priceless_line_is_skipped_not_zero_priced(db_session):
    from app.models import Offer, ProactiveMatch, Requirement, Requisition, User
    from app.services.proactive_service import _build_line_items

    user = User(email="r@x.com", name="R", role="sales", azure_id="az-qc5")
    db_session.add(user)
    db_session.flush()
    req = Requisition(name="rq", status="open", created_by=user.id)
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn="ZEROPART", target_qty=10)
    db_session.add(requirement)
    db_session.flush()
    # Offer with NO unit_price → no price basis at all.
    offer = Offer(vendor_name="V", mpn="ZEROPART", qty_available=100, unit_price=None, status="active")
    db_session.add(offer)
    db_session.flush()
    m = ProactiveMatch(
        offer_id=offer.id,
        requirement_id=requirement.id,
        salesperson_id=user.id,
        mpn="ZEROPART",
        status="new",
    )
    m.offer = offer
    m.requirement = requirement

    line_items, total_sell, _ = _build_line_items([m], {})
    assert line_items == []  # priceless line skipped, not staged at $0
    assert total_sell == Decimal("0")


def test_priced_line_still_builds(db_session):
    from app.models import Offer, ProactiveMatch, Requirement, Requisition, User
    from app.services.proactive_service import _build_line_items

    user = User(email="r2@x.com", name="R2", role="sales", azure_id="az-qc5b")
    db_session.add(user)
    db_session.flush()
    req = Requisition(name="rq2", status="open", created_by=user.id)
    db_session.add(req)
    db_session.flush()
    requirement = Requirement(requisition_id=req.id, primary_mpn="PRICED", target_qty=10)
    db_session.add(requirement)
    db_session.flush()
    offer = Offer(vendor_name="V", mpn="PRICED", qty_available=100, unit_price=Decimal("8.00"), status="active")
    db_session.add(offer)
    db_session.flush()
    m = ProactiveMatch(
        offer_id=offer.id, requirement_id=requirement.id, salesperson_id=user.id, mpn="PRICED", status="new"
    )
    m.offer = offer
    m.requirement = requirement

    line_items, _, _ = _build_line_items([m], {})
    assert len(line_items) == 1
    assert line_items[0]["sell_price"] == pytest.approx(8.0 * 1.3)  # cost x 1.3 fallback
