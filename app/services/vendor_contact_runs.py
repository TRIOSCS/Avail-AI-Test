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
synchronous path produced. The registry mechanics live in the shared
``app.utils.run_registry.RunRegistry`` — this module keeps only the outcome type and
the singleton.

Called by: routers/htmx/vendors.py (vendor_find_contacts, vendor_find_contacts_status,
           _run_vendor_find_contacts).
Depends on: app.utils.run_registry (RunRegistry), dataclasses (stdlib).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..utils.run_registry import RUNNING, RunRegistry

__all__ = ["RUNNING", "VendorContactRunOutcome", "vendor_contact_runs"]


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


# Process-wide singleton — import this, do not instantiate per call.
vendor_contact_runs: RunRegistry[VendorContactRunOutcome] = RunRegistry()
