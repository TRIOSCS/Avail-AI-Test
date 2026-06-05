"""Typed shapes for MaterialCard.enrichment_provenance (the JSONB column stays ``dict``
at the ORM layer; these constrain the producer functions under mypy)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NotRequired, TypedDict


@dataclass
class WebMeter:
    """Mutable per-card budget/health meter threaded through ``enrich_card``.

    ``web_calls`` counts billable web-search-enabled Claude tier attempts; it is
    RESERVED *before* each dispatch so a call that bills then raises is still counted.
    ``claude_ok`` latches True after any Claude call returns without raising. The worker
    uses ``web_calls`` for the daily web budget and ``claude_ok`` to reset its breaker.
    """

    web_calls: int = 0
    claude_ok: bool = False

    def reserve_web_call(self) -> None:
        """Count one billable web-search tier attempt.

        Call BEFORE the await.
        """
        self.web_calls += 1

    def mark_claude_ok(self) -> None:
        """Latch that a Claude call returned without raising.

        Call AFTER the await.
        """
        self.claude_ok = True


class FieldProvenance(TypedDict):
    """Per-field provenance entry stored under a field name key."""

    source: str
    confidence: float
    fetched_at: str
    matched_mpn: NotRequired[str]


class EnrichmentProvenance(TypedDict, total=False):
    """Top-level provenance dict stored in MaterialCard.enrichment_provenance."""

    reconfirm_needed: bool
    web_sourced: bool
    confidence: float
    source_urls: list[str]
    source_domains: list[str]
    fetched_at: str
    # plus per-field FieldProvenance entries keyed by field name (description, etc.)
