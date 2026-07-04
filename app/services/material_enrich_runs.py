"""material_enrich_runs.py — in-process registry of in-flight material-card enrichment
runs.

Card enrichment is triggered on demand from the material-detail "Enrich" button
(``routers/htmx/materials.py:enrich_material``), which now schedules the heavy
authoritative ladder + structured-spec pass (~30s) as a FastAPI background task and
returns the detail partial immediately. Because a *blocked / no-op* run leaves the
card's ``enrichment_status`` unchanged (``unenriched`` — indistinguishable from
"never enriched"), the enrich-status poller cannot tell "still running" from "failed"
by the status column alone. This registry carries that transient, per-card signal:

  * ``begin(id)``            — claim a run; ``False`` if one is already in flight
                              (this is the double-enqueue guard).
  * ``finish(id, blocked=)`` — record the terminal outcome for the poller to consume.
  * ``consume_outcome(id)``  — pop the outcome once (``"blocked"`` / ``"done"`` / ``None``).
  * ``clear(id)``            — drop any entry (used when the status column already went
                              terminal, so the poller no longer needs the signal).

In-memory + ``threading.Lock``: the app runs a single uvicorn worker and the background
tasks execute in that same process, so a module-level dict is sufficient. It resets
cleanly on restart — the only loss is a stale in-flight guard, which the next Enrich
click clears anyway.

Called by: routers/htmx/materials.py (enrich_material, material_enrich_status_partial,
           _run_card_enrichment).
Depends on: threading (stdlib) only.
"""

from __future__ import annotations

import threading

RUNNING = "running"
BLOCKED = "blocked"
DONE = "done"


class _EnrichRuns:
    """Thread-safe map of ``material_card_id -> run state`` for on-demand enrichment."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[int, str] = {}

    def begin(self, card_id: int) -> bool:
        """Claim a run for *card_id*. Returns ``False`` if one is already in flight.

        This is the double-enqueue guard: a second click while a run is ``RUNNING``
        must not stack another background enrichment on the same card.
        """
        with self._lock:
            if self._state.get(card_id) == RUNNING:
                return False
            self._state[card_id] = RUNNING
            return True

    def finish(self, card_id: int, *, blocked: bool) -> None:
        """Record a run's terminal outcome so the next poll can consume it."""
        with self._lock:
            self._state[card_id] = BLOCKED if blocked else DONE

    def is_running(self, card_id: int) -> bool:
        """True while a background run is in flight for *card_id*."""
        with self._lock:
            return self._state.get(card_id) == RUNNING

    def consume_outcome(self, card_id: int) -> str | None:
        """Pop a terminal outcome once (``"blocked"`` / ``"done"``); ``None`` otherwise.

        A ``RUNNING`` entry is left in place (the run has not finished yet).
        """
        with self._lock:
            state = self._state.get(card_id)
            if state in (BLOCKED, DONE):
                del self._state[card_id]
                return state
            return None

    def clear(self, card_id: int) -> None:
        """Drop any entry for *card_id* (idempotent)."""
        with self._lock:
            self._state.pop(card_id, None)


# Process-wide singleton — import this, do not instantiate per call.
enrich_runs = _EnrichRuns()
