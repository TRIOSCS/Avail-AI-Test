"""Typed shapes for MaterialCard.enrichment_provenance (the JSONB column stays ``dict``
at the ORM layer; these constrain the producer functions under mypy)."""

from __future__ import annotations

from typing import NotRequired, TypedDict


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
