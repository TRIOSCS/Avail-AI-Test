"""vendor_contact_runs.py — in-process registry of in-flight vendor "Find Contacts"
runs.

The vendor-detail "Find Contacts" tab triggers an AI web-search contact discovery
(``ai_service.enrich_contacts_websearch``: Claude + the ``web_search`` tool, commonly
>15s). Run inline it blew past htmx's 15s client timeout and the tab spun then errored out.
The HTMX path now schedules that heavy call as a FastAPI background task and returns a
"Finding contacts…" poller immediately, so the click never blocks.

There is no persisted column that flips when the search finishes — discovered contacts are
appended as ``ProspectContact`` rows, and a legitimate "no contacts found" run appends none
(indistinguishable from "never ran" by the table alone) — so the poller cannot read a DB
status to tell "still running" from "finished". This registry carries that transient,
per-vendor signal AND the run's *outcome* (how many NEW prospects were saved, or the error
message) so the poller can render the same results / none-found / error panel the old
synchronous path produced:

  * ``begin(id)``            — claim a run; ``False`` if one is already in flight
                              (this is the double-enqueue guard).
  * ``finish(id, outcome)``  — record the terminal ``VendorContactRunOutcome`` for the poller.
  * ``is_running(id)``       — ``True`` while a background run is in flight.
  * ``consume_outcome(id)``  — pop the outcome once (``VendorContactRunOutcome`` / ``None``).
  * ``clear(id)``            — drop any entry (idempotent).

In-memory + ``threading.Lock``: the app runs a single uvicorn worker and the background
tasks execute in that same process, so a module-level dict is sufficient. It resets cleanly
on restart — the only loss is a stale in-flight guard, which the next click clears.

Called by: routers/htmx/vendors.py (vendor_find_contacts, vendor_find_contacts_status,
           _run_vendor_find_contacts).
Depends on: dataclasses + threading (stdlib) only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

RUNNING = "running"


@dataclass(frozen=True)
class VendorContactRunOutcome:
    """Terminal result of one background vendor "Find Contacts" run.

    ``error`` is set to a human-readable message only when the web search itself failed —
    the poller surfaces the rose error panel. On success it is ``None`` and ``new_count`` is
    how many NEW ``ProspectContact`` rows were saved; ``0`` renders the amber "no contacts
    found" state.
    """

    new_count: int = 0
    error: str | None = None


class _VendorContactRuns:
    """Thread-safe map of ``vendor_card_id -> run state`` for on-demand contact
    discovery.

    A value is either the ``RUNNING`` sentinel (in flight) or a ``VendorContactRunOutcome``
    (finished, awaiting consumption by the poller).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[int, str | VendorContactRunOutcome] = {}

    def begin(self, vendor_id: int) -> bool:
        """Claim a run for *vendor_id*. Returns ``False`` if one is already in flight.

        This is the double-enqueue guard: a second click while a run is ``RUNNING`` must not
        stack another background search on the same vendor.
        """
        with self._lock:
            if self._state.get(vendor_id) == RUNNING:
                return False
            self._state[vendor_id] = RUNNING
            return True

    def finish(self, vendor_id: int, outcome: VendorContactRunOutcome) -> None:
        """Record a run's terminal outcome so the next poll can consume it."""
        with self._lock:
            self._state[vendor_id] = outcome

    def is_running(self, vendor_id: int) -> bool:
        """True while a background run is in flight for *vendor_id*."""
        with self._lock:
            return self._state.get(vendor_id) == RUNNING

    def consume_outcome(self, vendor_id: int) -> VendorContactRunOutcome | None:
        """Pop a terminal outcome once; ``None`` while still running or already
        consumed.

        A ``RUNNING`` entry is left in place (the run has not finished yet).
        """
        with self._lock:
            state = self._state.get(vendor_id)
            if isinstance(state, VendorContactRunOutcome):
                del self._state[vendor_id]
                return state
            return None

    def clear(self, vendor_id: int) -> None:
        """Drop any entry for *vendor_id* (idempotent)."""
        with self._lock:
            self._state.pop(vendor_id, None)


# Process-wide singleton — import this, do not instantiate per call.
vendor_contact_runs = _VendorContactRuns()
