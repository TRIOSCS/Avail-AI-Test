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
``_enrich_result.html`` panel the old synchronous path produced. The registry mechanics
(begin / finish / is_running / consume_outcome / clear, the RUNNING sentinel, the
threading.Lock) live in the shared ``app.utils.run_registry.RunRegistry`` — this module
keeps only the outcome type and the singleton.

Called by: routers/crm/enrichment.py (enrich_company, enrich_company_status) and
           services/customer_enrichment_service.py (run_company_enrichment).
Depends on: app.utils.run_registry (RunRegistry), dataclasses (stdlib).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils.run_registry import RUNNING, RunRegistry

__all__ = ["RUNNING", "CompanyEnrichOutcome", "company_enrich_runs"]


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


# Process-wide singleton — import this, do not instantiate per call.
company_enrich_runs: RunRegistry[CompanyEnrichOutcome] = RunRegistry()
