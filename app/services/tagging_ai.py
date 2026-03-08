"""AI fallback classification — re-export façade.

Split into domain modules:
  - tagging_ai_classify: prompt + real-time classify + apply helpers
  - tagging_ai_batch: Batch API submit/apply/check + targeted backfill
  - tagging_ai_triage: internal part heuristic + AI triage

All public names re-exported here for backward compatibility.
"""

# ── Classify ──────────────────────────────────────────────────────────
# ── Batch ─────────────────────────────────────────────────────────────
from app.services.tagging_ai_batch import (  # noqa: F401
    _apply_chunked_batch,
    apply_batch_results_chunked,
    check_and_apply_batch_results,
    run_ai_backfill,
    submit_batch_backfill,
    submit_targeted_backfill,
)
from app.services.tagging_ai_classify import (  # noqa: F401
    _CLASSIFY_PROMPT,
    _SYSTEM,
    _apply_ai_results,
    classify_parts_with_ai,
)

# ── Triage ────────────────────────────────────────────────────────────
from app.services.tagging_ai_triage import (  # noqa: F401
    apply_triage_results,
    submit_triage_batch,
    triage_internal_parts,
)
