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
synchronous path produced. The registry mechanics live in the shared
``app.utils.run_registry.RunRegistry`` — this module keeps only the outcome type and
the singleton.

This is a SEPARATE registry from ``company_enrich_runs`` on purpose: the header "Enrich"
button uses that one, and sharing a key would make the two buttons block each other on the
same company (a Find-contacts run in flight would falsely report the account as "enriching",
and vice versa).

Called by: routers/htmx/companies.py (contacts_tab_suggested,
           contacts_tab_suggested_status, _run_contact_discovery).
Depends on: app.utils.run_registry (RunRegistry), dataclasses (stdlib).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils.run_registry import RUNNING, RunRegistry

__all__ = ["RUNNING", "ContactDiscoveryOutcome", "contact_discovery_runs"]


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


# Process-wide singleton — import this, do not instantiate per call.
contact_discovery_runs: RunRegistry[ContactDiscoveryOutcome] = RunRegistry()
