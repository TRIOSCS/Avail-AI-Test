"""test_connectors_test_all_concurrency.py — Phase-0 FIX B: the Connectors "Test all"
sweep runs probes CONCURRENTLY within a bounded budget and aborts when the client
disconnects.

Before this fix the sweep awaited run_source_test per source SEQUENTIALLY; with >4 live
connectors (15-30s each) it blew the client's 15s htmx timeout → htmx aborted the XHR,
no OOB cards/summary were applied, and the server kept burning paid quota on discarded
results.

Covers:
- probes overlap (max observed concurrency > 1; wall time << sum of probe times)
- all testable+active sources produce a card (tested_count == N)
- request.is_disconnected() aborts the sweep promptly without waiting out the probes

Called by: pytest
Depends on: app/routers/htmx/settings.py, app/routers/sources.py, tests/conftest.py
"""

import asyncio
import os
import time

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock

import app.routers.htmx.settings as st
import app.routers.sources as rs
from app.models import ApiSource
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used


def _mk_active_source(db, name):
    src = ApiSource(
        name=name,
        display_name=name,
        category="market_data",
        source_type="api",
        status="pending",
        env_vars=[],
        is_active=True,
        total_searches=0,
        total_results=0,
        avg_response_ms=0,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


def _patch_common(monkeypatch, captured):
    """Mark every source testable, stub out render + base ctx to avoid a full template
    render while capturing the response context."""
    monkeypatch.setattr(st, "_enrich_source", lambda src, db: {"testable": True, "state": "live", "name": src.name})
    monkeypatch.setattr(st, "_base_ctx", lambda req, user, view: {})
    monkeypatch.setattr(st, "template_response", lambda name, ctx: (captured.update(ctx), "OK")[1])


async def test_probes_run_concurrently_within_budget(db_session, monkeypatch):
    """6 stubbed probes (0.3s each) overlap — wall time is ~one probe, not six, and the
    max observed concurrency proves they ran together (was strictly sequential)."""
    for i in range(6):
        _mk_active_source(db_session, f"conc_src_{i}")

    gauge = {"cur": 0, "max": 0}

    async def _stub_probe(src, db):
        gauge["cur"] += 1
        gauge["max"] = max(gauge["max"], gauge["cur"])
        await asyncio.sleep(0.3)
        gauge["cur"] -= 1
        return {"results": [{"vendor_name": "x"}], "elapsed_ms": 300, "error": None}

    monkeypatch.setattr(rs, "_probe_source", _stub_probe)
    captured: dict = {}
    _patch_common(monkeypatch, captured)

    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=False)

    t0 = time.monotonic()
    await st.connectors_test_all(req, user=MagicMock(), db=db_session)
    elapsed = time.monotonic() - t0

    assert captured["tested_count"] == 6, "every testable+active source must produce a card"
    assert captured["failed_count"] == 0
    assert gauge["max"] >= 5, f"probes did not overlap (max concurrency={gauge['max']})"
    assert elapsed < 1.5, f"sequential would be ~1.8s; concurrent should be ~0.3s (got {elapsed:.2f}s)"


async def test_is_disconnected_aborts_sweep(db_session, monkeypatch):
    """When the client is already gone, the sweep cancels in-flight probes and returns
    promptly instead of waiting out the (5s) probes — so it stops burning paid quota."""
    for i in range(4):
        _mk_active_source(db_session, f"abort_src_{i}")

    async def _slow_probe(src, db):
        await asyncio.sleep(5.0)  # long — must be cancelled, not awaited
        return {"results": [], "elapsed_ms": 5000, "error": None}

    monkeypatch.setattr(rs, "_probe_source", _slow_probe)
    captured: dict = {}
    _patch_common(monkeypatch, captured)

    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=True)  # client abandoned the request

    t0 = time.monotonic()
    await st.connectors_test_all(req, user=MagicMock(), db=db_session)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"disconnect must abort promptly, not wait out the probes (got {elapsed:.2f}s)"
    assert captured["tested_count"] == 0, "cancelled probes must not be persisted/rendered"
