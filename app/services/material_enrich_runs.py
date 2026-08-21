"""material_enrich_runs.py — in-process registries of in-flight material-card background
runs (on-demand enrichment AND the AI crosses/substitutes lookup).

Both actions are triggered on demand from the material-detail panel — the "Enrich"
button (``routers/htmx/materials.py:enrich_material``) and the "Find Crosses" / "Refresh"
button (``find_crosses``) — and both now schedule their heavy Claude call (~30s) as a
FastAPI background task and return a polling partial immediately. Because a *blocked /
no-op* run leaves the card's persisted state indistinguishable from "never ran"
(enrichment: ``enrichment_status`` stays ``unenriched``; crosses: ``cross_references``
stays empty on a legitimate no-results run), the status poller cannot tell "still
running" from "failed / done" by the column alone. Each registry carries that transient,
per-card signal; the registry mechanics live in the shared
``app.utils.run_registry.RunRegistry`` — this module keeps the string outcome values
(``BLOCKED`` / ``DONE``), the historical ``finish(id, *, blocked=)`` signature, and the
two singletons.

Two independent singletons share the registry class so enrichment and crosses never
collide on a card id: ``enrich_runs`` (enrichment) and ``crosses_runs`` (AI crosses
lookup).

Called by: routers/htmx/materials.py (enrich_material, material_enrich_status_partial,
           _run_card_enrichment, find_crosses, material_crosses_status_partial,
           _run_card_crosses).
Depends on: app.utils.run_registry (RunRegistry).
"""

from __future__ import annotations

from ..utils.run_registry import RUNNING, RunRegistry

__all__ = ["BLOCKED", "DONE", "RUNNING", "crosses_runs", "enrich_runs"]

BLOCKED = "blocked"
DONE = "done"


class _MaterialRunRegistry(RunRegistry[str]):
    """RunRegistry with the material poller's historical string outcomes.

    Keeps the pre-refactor ``finish(card_id, *, blocked=...)`` keyword signature and
    maps it onto the generic outcome slot (``"blocked"`` / ``"done"``).
    """

    def finish(self, card_id: int, *, blocked: bool) -> None:  # type: ignore[override]
        """Record a run's terminal outcome so the next poll can consume it."""
        super().finish(card_id, BLOCKED if blocked else DONE)


# Process-wide singletons — import these, do not instantiate per call. Enrichment and the
# AI crosses lookup keep separate registries so a run of one never masks the other.
enrich_runs = _MaterialRunRegistry()
crosses_runs = _MaterialRunRegistry()
