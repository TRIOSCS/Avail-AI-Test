"""company_enrich_runs.py — in-process registry of in-flight account (Company)
enrichment runs.

Account enrichment is triggered on demand from the customer-detail "Enrich" button
(``routers/crm/enrichment.py:enrich_company``). The HTMX path now schedules the heavy
external-provider waterfall — firmographics (``enrich_entity``: SAM.gov + Clay/Explorium/
Lusha + Anthropic, ~15-40s) plus contact discovery (``find_suggested_contacts_with_errors``:
Hunter/Clay) — as a FastAPI background task and returns an "Enriching…" poller immediately,
so the click never blocks.

Unlike a material card there is no ``enrichment_status`` column that flips on completion, so
the poller cannot read a DB status to tell "still running" from "finished". This registry
carries that transient, per-account signal AND the run's *result* (which fields changed, the
discovered contacts, which providers errored) so the poller can render the same
``_enrich_result.html`` panel the old synchronous path produced:

  * ``begin(id)``            — claim a run; ``False`` if one is already in flight
                              (this is the double-enqueue guard).
  * ``finish(id, outcome)``  — record the terminal ``CompanyEnrichOutcome`` for the poller.
  * ``is_running(id)``       — ``True`` while a background run is in flight.
  * ``consume_outcome(id)``  — pop the outcome once (``CompanyEnrichOutcome`` / ``None``).
  * ``clear(id)``            — drop any entry (idempotent).

In-memory + ``threading.Lock``: the app runs a single uvicorn worker and the background
tasks execute in that same process, so a module-level dict is sufficient. It resets cleanly
on restart — the only loss is a stale in-flight guard, which the next Enrich click clears.

Called by: routers/crm/enrichment.py (enrich_company, enrich_company_status,
           _run_company_enrichment).
Depends on: dataclasses + threading (stdlib) only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

RUNNING = "running"


@dataclass(frozen=True)
class CompanyEnrichOutcome:
    """Terminal result of one background account-enrichment run.

    ``blocked`` is True only when the firmographics pass (``enrich_entity``) could not
    complete — a genuinely-unavailable data source — which the poller surfaces as a
    "couldn't complete" toast. A contact-discovery hiccup is NOT ``blocked``: it degrades
    to the amber "couldn't reach <provider>" banner inside the panel via
    ``errored_providers`` (mirrors the old synchronous graceful-degradation behavior).
    """

    blocked: bool = False
    updated_fields: list[str] = field(default_factory=list)
    suggested: list[dict] = field(default_factory=list)
    errored_providers: list[str] = field(default_factory=list)


class _CompanyEnrichRuns:
    """Thread-safe map of ``company_id -> run state`` for on-demand account enrichment.

    A value is either the ``RUNNING`` sentinel (in flight) or a ``CompanyEnrichOutcome``
    (finished, awaiting consumption by the poller).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[int, str | CompanyEnrichOutcome] = {}

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

    def finish(self, company_id: int, outcome: CompanyEnrichOutcome) -> None:
        """Record a run's terminal outcome so the next poll can consume it."""
        with self._lock:
            self._state[company_id] = outcome

    def is_running(self, company_id: int) -> bool:
        """True while a background run is in flight for *company_id*."""
        with self._lock:
            return self._state.get(company_id) == RUNNING

    def consume_outcome(self, company_id: int) -> CompanyEnrichOutcome | None:
        """Pop a terminal outcome once; ``None`` while still running or already
        consumed.

        A ``RUNNING`` entry is left in place (the run has not finished yet).
        """
        with self._lock:
            state = self._state.get(company_id)
            if isinstance(state, CompanyEnrichOutcome):
                del self._state[company_id]
                return state
            return None

    def clear(self, company_id: int) -> None:
        """Drop any entry for *company_id* (idempotent)."""
        with self._lock:
            self._state.pop(company_id, None)


# Process-wide singleton — import this, do not instantiate per call.
company_enrich_runs = _CompanyEnrichRuns()
