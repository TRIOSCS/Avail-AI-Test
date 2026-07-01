"""Parity tests for the nc/ics/tbf AI-gate shims delegating to the base AIGate.

Proves the de-duplication refactor preserved behavior: each worker module
re-exports the base gate's shared cache state, routes classification through the
patchable module-level function, keeps module-level cooldown state in sync, and
preserves NetComponents' priority-first pending ordering (ics/tbf stay
oldest-first).

Called by: pytest
Depends on: the three worker ai_gate shims + search_worker_base.ai_gate.AIGate
"""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ["TESTING"] = "1"

import app.services.ics_worker.ai_gate as ics_gate
import app.services.nc_worker.ai_gate as nc_gate
import app.services.tbf_worker.ai_gate as tbf_gate
from app.models import IcsSearchQueue, NcSearchQueue, TbfSearchQueue
from app.services.search_worker_base.ai_gate import AIGate

ALL_SHIMS = [nc_gate, ics_gate, tbf_gate]


@pytest.mark.parametrize("mod", ALL_SHIMS)
def test_shim_wraps_a_base_gate(mod):
    assert isinstance(mod._gate, AIGate)


@pytest.mark.parametrize("mod", ALL_SHIMS)
def test_cache_state_is_shared_with_base_gate(mod):
    # Same objects, so mutations through the module names hit the gate's cache.
    assert mod._classification_cache is mod._gate._classification_cache
    assert mod._cache_lock is mod._gate._cache_lock


@pytest.mark.parametrize(
    "mod,model,marketplace,field",
    [
        (nc_gate, NcSearchQueue, "NetComponents", "search_nc"),
        (ics_gate, IcsSearchQueue, "ICsource", "search_ics"),
        (tbf_gate, TbfSearchQueue, "a broker", "search_broker"),
    ],
)
def test_gate_configured_for_its_marketplace(mod, model, marketplace, field):
    assert mod._gate.queue_model is model
    assert mod._gate.marketplace_name == marketplace
    assert mod._gate.search_field == field


def test_nc_orders_pending_priority_first_then_newest():
    """NetComponents preserves its distinct priority.asc(), created_at.desc() order."""
    assert nc_gate._gate._order_by is not None
    # Compare against freshly-built expected clauses by their SQL string form.
    expected = [NcSearchQueue.priority.asc(), NcSearchQueue.created_at.desc()]
    assert [str(c) for c in nc_gate._gate._order_by] == [str(c) for c in expected]


@pytest.mark.parametrize("mod", [ics_gate, tbf_gate])
def test_ics_and_tbf_use_default_oldest_first_order(mod):
    """ICsource/TBF keep the base default (created_at.asc()) — no custom order."""
    assert mod._gate._order_by is None


@pytest.mark.parametrize("mod", ALL_SHIMS)
def test_process_ai_gate_applies_expected_order_by(mod):
    """The exact order-by clauses reach db.query(...).order_by(...) unchanged."""
    captured = {}

    def capture_order_by(*clauses):
        captured["clauses"] = clauses
        chained = MagicMock()
        chained.limit.return_value.all.return_value = []  # no pending -> early return
        return chained

    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.side_effect = capture_order_by

    import asyncio

    asyncio.run(mod.process_ai_gate(db))

    model = mod._gate.queue_model
    if mod is nc_gate:
        expected = [model.priority.asc(), model.created_at.desc()]
    else:
        expected = [model.created_at.asc()]
    assert [str(c) for c in captured["clauses"]] == [str(c) for c in expected]


@pytest.mark.parametrize("mod", ALL_SHIMS)
def test_cooldown_state_syncs_module_to_gate(mod):
    """Setting the module-level cooldown makes the shared gate skip processing."""
    import time

    original = mod._last_api_failure
    try:
        mod._last_api_failure = time.monotonic()
        db = MagicMock()
        import asyncio

        asyncio.run(mod.process_ai_gate(db))
        db.query.assert_not_called()  # cooldown honored via synced state
    finally:
        mod._last_api_failure = original


@pytest.mark.parametrize("mod", ALL_SHIMS)
def test_module_classify_patch_is_used_by_base_gate(mod, monkeypatch):
    """Patching the module-level classify_parts_batch still drives the gate.

    A None result must fail-open the single pending item to 'queued'.
    """
    import asyncio

    mod.clear_classification_cache()
    original = mod._last_api_failure
    mod._last_api_failure = 0.0
    try:
        item = MagicMock()
        item.mpn = "LM358"
        item.normalized_mpn = "lm358"
        item.manufacturer = "TI"
        item.description = "op-amp"
        item.status = "pending"
        item.updated_at = None

        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [item]

        monkeypatch.setattr(mod, "classify_parts_batch", AsyncMock(return_value=None))
        asyncio.run(mod.process_ai_gate(db))

        assert item.status == "queued"
        assert item.gate_decision == "search"
        assert mod._last_api_failure > 0  # synced back out of the gate
    finally:
        mod._last_api_failure = original
        mod.clear_classification_cache()
