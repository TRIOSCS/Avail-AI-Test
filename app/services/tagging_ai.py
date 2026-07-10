"""AI fallback classification — re-export façade.

Split into domain modules:
  - tagging_ai_classify: prompt + real-time classify + apply helpers
  - tagging_ai_batch: Batch API submit/apply/check + targeted backfill
  - tagging_ai_triage: internal part heuristic + AI triage

All public names re-exported here for backward compatibility.
"""

# ── Classify ──────────────────────────────────────────────────────────
# ── Batch ─────────────────────────────────────────────────────────────
from app.services.tagging_ai_batch import (
    _apply_chunked_batch,  # noqa: F401
    apply_batch_results_chunked,  # noqa: F401
    check_and_apply_batch_results,  # noqa: F401
    run_ai_backfill,  # noqa: F401
    submit_batch_backfill,  # noqa: F401
    submit_targeted_backfill,  # noqa: F401
)
from app.services.tagging_ai_classify import (
    _CLASSIFY_PROMPT,  # noqa: F401
    _SYSTEM,  # noqa: F401
    _apply_ai_results,  # noqa: F401
    classify_parts_with_ai,  # noqa: F401
)

# ── Triage ────────────────────────────────────────────────────────────
from app.services.tagging_ai_triage import (
    apply_triage_results,  # noqa: F401
    submit_triage_batch,  # noqa: F401
    triage_internal_parts,  # noqa: F401
)
