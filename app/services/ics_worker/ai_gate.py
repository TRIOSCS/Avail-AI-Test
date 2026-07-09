"""AI commodity gate for ICsource search queue.

Thin worker-specific shim over the parameterized ``AIGate`` in
``app/services/search_worker_base/ai_gate.py``. It wires the shared base
implementation to the ICsource queue (``IcsSearchQueue``), marketplace name,
and ``search_ics`` field, then re-exports the public ``process_ai_gate`` /
``classify_parts_batch`` / ``clear_classification_cache`` surface (plus the
module-level ``_classification_cache`` / ``_cache_lock`` / ``_last_api_failure``
state that callers and tests reach into).

Called by: worker loop (process_ai_gate)
Depends on: search_worker_base.ai_gate.AIGate, IcsSearchQueue model
"""

from sqlalchemy.orm import Session

from app.models import IcsSearchQueue
from app.services.search_worker_base.ai_gate import AIGate

# Single shared gate instance carrying the ICsource-specific config. ICsource
# orders pending items oldest-first (the base default).
_gate = AIGate(
    IcsSearchQueue,
    marketplace_name="ICsource",
    search_field="search_ics",
    log_prefix="ICS",
)

# Re-export the base instance's cache state as module-level names so existing
# callers/tests that mutate ``_classification_cache`` / hold ``_cache_lock``
# operate on the SAME objects the gate uses.
_classification_cache = _gate._classification_cache
_cache_lock = _gate._cache_lock

# Module-level cooldown timestamp. Tests set/read this directly, so it is kept
# in sync with the gate's own ``_last_api_failure`` inside ``process_ai_gate``.
_last_api_failure: float = 0.0


async def classify_parts_batch(parts: list[dict]) -> list[dict] | None:
    """Classify up to 30 parts using Claude Haiku (delegates to the base gate)."""
    return await AIGate.classify_parts_batch(_gate, parts)


async def _instance_classify(parts: list[dict]) -> list[dict] | None:
    """Indirection the base gate calls so tests patching the module-level
    ``classify_parts_batch`` still take effect."""
    return await classify_parts_batch(parts)


# Route the gate's classification through the module-level (patchable) function.
_gate.classify_parts_batch = _instance_classify


async def process_ai_gate(db: Session):
    """Process pending queue items through the AI classification gate.

    Delegates to the shared base gate, syncing the module-level cooldown
    timestamp in and back out so tests (and callers) that read/write
    ``_last_api_failure`` observe consistent state.
    """
    global _last_api_failure
    _gate._last_api_failure = _last_api_failure
    try:
        await _gate.process_ai_gate(db)
    finally:
        _last_api_failure = _gate._last_api_failure


def clear_classification_cache():
    """Clear the in-memory classification cache (for testing)."""
    with _cache_lock:
        _classification_cache.clear()
