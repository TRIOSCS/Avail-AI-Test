"""run_registry.py — generic thread-safe in-process registry of in-flight background
runs.

Several on-demand HTMX actions (account enrichment, account contact discovery, vendor
"Find Contacts", material enrichment / AI crosses) schedule a heavy external call as a
FastAPI background task and return a poller immediately. Because no persisted column
reliably flips when such a run finishes, each feature keeps an in-process registry that
carries the transient "still running" signal plus the run's terminal outcome for the
poller to consume. The mechanics are identical everywhere; only the outcome type and
the keyed entity differ — this class is the ONE implementation they all share:

  * ``begin(id)``            — claim a run; ``False`` if one is already in flight
                              (this is the double-enqueue guard).
  * ``finish(id, outcome)``  — record the terminal outcome for the poller to consume.
  * ``is_running(id)``       — ``True`` while a background run is in flight.
  * ``consume_outcome(id)``  — pop the outcome once (outcome / ``None``).
  * ``clear(id)``            — drop any entry (idempotent).

In-memory + ``threading.Lock``: the app runs a single uvicorn worker and the background
tasks execute in that same process, so a module-level dict is sufficient. It resets
cleanly on restart — the only loss is a stale in-flight guard, which the next click
clears.

Called by: services/company_enrich_runs.py, services/contact_discovery_runs.py,
           services/material_enrich_runs.py, services/vendor_contact_runs.py (each
           instantiates its own module-level singleton(s)).
Depends on: threading (stdlib) only.
"""

from __future__ import annotations

import threading
from typing import cast

RUNNING = "running"


class RunRegistry[T]:
    """Thread-safe map of ``entity_id -> run state`` for one kind of on-demand run.

    A value is either the ``RUNNING`` sentinel (in flight) or a terminal outcome of
    type ``T`` (finished, awaiting consumption by the poller).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[int, str | T] = {}

    def begin(self, entity_id: int) -> bool:
        """Claim a run for *entity_id*. Returns ``False`` if one is already in flight.

        This is the double-enqueue guard: a second click while a run is ``RUNNING``
        must not stack another background run on the same entity.
        """
        with self._lock:
            if self._state.get(entity_id) == RUNNING:
                return False
            self._state[entity_id] = RUNNING
            return True

    def finish(self, entity_id: int, outcome: T) -> None:
        """Record a run's terminal outcome so the next poll can consume it."""
        with self._lock:
            self._state[entity_id] = outcome

    def is_running(self, entity_id: int) -> bool:
        """True while a background run is in flight for *entity_id*."""
        with self._lock:
            return self._state.get(entity_id) == RUNNING

    def consume_outcome(self, entity_id: int) -> T | None:
        """Pop a terminal outcome once; ``None`` while still running or already
        consumed.

        A ``RUNNING`` entry is left in place (the run has not finished yet).
        """
        with self._lock:
            state = self._state.get(entity_id)
            if state is None or state == RUNNING:
                return None
            del self._state[entity_id]
            return cast("T", state)

    def clear(self, entity_id: int) -> None:
        """Drop any entry for *entity_id* (idempotent)."""
        with self._lock:
            self._state.pop(entity_id, None)
