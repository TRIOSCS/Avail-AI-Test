"""contact_discovery_runs.py — in-process registry of in-flight account (Company)
contact-discovery runs.

Contact discovery is triggered on demand from the customer-detail Contacts tab
"Find contacts" button (``routers/htmx/companies.py:contacts_tab_suggested``). That
HTMX path now schedules the multi-provider suggested-contacts waterfall
(``find_suggested_contacts_with_errors``: Hunter/Clay/Lusha/Explorium, ~10-40s) as a
FastAPI background task and returns a "Finding contacts…" poller immediately, so the
click never blocks.

There is no DB column that flips when discovery finishes, so the poller cannot read a
status to tell "still running" from "finished". This registry carries that transient,
per-account signal AND the run's *result* (the discovered contacts + which providers
errored) so the poller can render the same ``_suggested_contacts.html`` panel the old
synchronous path produced:

  * ``begin(id)``            — claim a run; ``False`` if one is already in flight
                              (this is the double-enqueue guard).
  * ``finish(id, outcome)``  — record the terminal ``ContactDiscoveryOutcome``.
  * ``is_running(id)``       — ``True`` while a background run is in flight.
  * ``consume_outcome(id)``  — pop the outcome once (``ContactDiscoveryOutcome`` / ``None``).
  * ``clear(id)``            — drop any entry (idempotent).

This is a SEPARATE registry from ``company_enrich_runs`` on purpose: the header "Enrich"
button uses that one, and sharing a key would make the two buttons block each other on the
same company (a Find-contacts run in flight would falsely report the account as "enriching",
and vice versa).

In-memory + ``threading.Lock``: the app runs a single uvicorn worker and the background
tasks execute in that same process, so a module-level dict is sufficient. It resets cleanly
on restart — the only loss is a stale in-flight guard, which the next click clears.

Called by: routers/htmx/companies.py (contacts_tab_suggested,
           contacts_tab_suggested_status, _run_contact_discovery).
Depends on: dataclasses + threading (stdlib) only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

RUNNING = "running"


@dataclass(frozen=True)
class ContactDiscoveryOutcome:
    """Terminal result of one background contact-discovery run.

    ``suggested`` is the discovered-contacts list; ``errored_providers`` names any
    metered provider that tripped (quota/rate-limit) or ``["all"]`` for a whole-waterfall
    failure — the poller surfaces those as the amber "couldn't reach" banner, exactly as
    the old synchronous path did. There is no "blocked" toast: contact discovery only ever
    degrades gracefully.
    """

    suggested: list[dict] = field(default_factory=list)
    errored_providers: list[str] = field(default_factory=list)


class _ContactDiscoveryRuns:
    """Thread-safe map of ``company_id -> run state`` for on-demand contact discovery.

    A value is either the ``RUNNING`` sentinel (in flight) or a ``ContactDiscoveryOutcome``
    (finished, awaiting consumption by the poller).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[int, str | ContactDiscoveryOutcome] = {}

    def begin(self, company_id: int) -> bool:
        """Claim a run for *company_id*. Returns ``False`` if one is already in flight.

        This is the double-enqueue guard: a second click while a run is ``RUNNING``
        must not stack another background waterfall on the same account.
        """
        with self._lock:
            if self._state.get(company_id) == RUNNING:
                return False
            self._state[company_id] = RUNNING
            return True

    def finish(self, company_id: int, outcome: ContactDiscoveryOutcome) -> None:
        """Record a run's terminal outcome so the next poll can consume it."""
        with self._lock:
            self._state[company_id] = outcome

    def is_running(self, company_id: int) -> bool:
        """True while a background run is in flight for *company_id*."""
        with self._lock:
            return self._state.get(company_id) == RUNNING

    def consume_outcome(self, company_id: int) -> ContactDiscoveryOutcome | None:
        """Pop a terminal outcome once; ``None`` while still running or already
        consumed.

        A ``RUNNING`` entry is left in place (the run has not finished yet).
        """
        with self._lock:
            state = self._state.get(company_id)
            if isinstance(state, ContactDiscoveryOutcome):
                del self._state[company_id]
                return state
            return None

    def clear(self, company_id: int) -> None:
        """Drop any entry for *company_id* (idempotent)."""
        with self._lock:
            self._state.pop(company_id, None)


# Process-wide singleton — import this, do not instantiate per call.
contact_discovery_runs = _ContactDiscoveryRuns()
