"""SP-Ingest consolidation — merge all cleaned records of one MPN into a
ConsolidatedPart.

What: ``consolidate`` groups cleaned SourceRecords by ``normalized_mpn`` and picks the best
      value per field with provenance — description = longest non-empty; manufacturer = modal
      (most common) non-empty; category = the highest-priority source-kind's non-empty value
      (sfdc_master > inventory_sheet unconditionally, first-seen within a kind); condition =
      most common; quantity = sum; specs = merged with SFDC-master values winning over
      inventory-sheet values. For the modal/longest fields, source-kind priority
      (sfdc_master > inventory_sheet) is only a tie-break.
Called by: app/management/ingest_source_data.py (after clean.py, before ai_correct/ingest).
Depends on: the SourceRecord / ConsolidatedPart dataclasses + SOURCE_KIND_PRIORITY (no DB,
      no app services — a pure transform over the cleaned records).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from loguru import logger

from app.services.source_ingest.models import (
    SOURCE_KIND_PRIORITY,
    ConsolidatedPart,
    SourceRecord,
)


def _modal_nonempty(values: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Return (most-common non-empty value, source_kind that supplied it).

    *values* is a list of (value, source_kind). Ties on count break by source-kind
    priority (sfdc_master > inventory_sheet), then by first-seen order. Returns (None,
    None) when no non-empty value exists.
    """
    counts: Counter[str] = Counter(v for v, _ in values if v)
    if not counts:
        return None, None
    top = max(counts.values())
    winners = [v for v, c in counts.items() if c == top]
    # Tie-break by best source-kind priority among the records carrying each winner.
    best_value: str | None = None
    best_priority = -1
    best_kind: str | None = None
    for value, kind in values:
        if value not in winners:
            continue
        prio = SOURCE_KIND_PRIORITY.get(kind, 0)
        if prio > best_priority:
            best_priority = prio
            best_value = value
            best_kind = kind
    return best_value, best_kind


def _longest_nonempty(values: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Return (longest non-empty value, source_kind).

    Ties break by source-kind priority.
    """
    best_value: str | None = None
    best_kind: str | None = None
    best_len = -1
    best_priority = -1
    for value, kind in values:
        if not value:
            continue
        prio = SOURCE_KIND_PRIORITY.get(kind, 0)
        if len(value) > best_len or (len(value) == best_len and prio > best_priority):
            best_len = len(value)
            best_priority = prio
            best_value = value
            best_kind = kind
    return best_value, best_kind


def _first_nonempty(values: list[tuple[str, str]]) -> tuple[str | None, str | None]:
    """Return (first non-empty value, source_kind), preferring higher source-kind
    priority."""
    # Stable: among records with a value, prefer the highest-priority source-kind, else first.
    best_value: str | None = None
    best_kind: str | None = None
    best_priority = -1
    for value, kind in values:
        if not value:
            continue
        prio = SOURCE_KIND_PRIORITY.get(kind, 0)
        if prio > best_priority:
            best_priority = prio
            best_value = value
            best_kind = kind
    return best_value, best_kind


def _consolidate_group(records: list[SourceRecord]) -> ConsolidatedPart:
    """Merge one MPN's records into a ConsolidatedPart with per-field provenance."""
    field_sources: dict[str, str] = {}

    desc, desc_kind = _longest_nonempty([(r.description or "", r.source_kind) for r in records])
    if desc_kind:
        field_sources["description"] = desc_kind

    mfr, mfr_kind = _modal_nonempty([(r.manufacturer or "", r.source_kind) for r in records])
    if mfr_kind:
        field_sources["manufacturer"] = mfr_kind

    cat, cat_kind = _first_nonempty([(r.category or "", r.source_kind) for r in records])
    if cat_kind:
        field_sources["category"] = cat_kind

    cond, cond_kind = _modal_nonempty([(r.condition or "", r.source_kind) for r in records])
    if cond_kind:
        field_sources["condition"] = cond_kind

    qty_values = [r.quantity for r in records if r.quantity is not None]
    quantity = sum(qty_values) if qty_values else None
    if quantity is not None:
        field_sources["quantity"] = "merged_sum"

    # Specs: merge across records; SFDC-master values win over inventory-sheet values for the
    # same spec_key. Records are sorted so master writes last (and thus wins the dict update).
    merged_specs: dict = {}
    for rec in sorted(records, key=lambda r: SOURCE_KIND_PRIORITY.get(r.source_kind, 0)):
        for key, value in rec.specs.items():
            if value is not None and str(value).strip():
                merged_specs[key] = value
                field_sources[f"spec:{key}"] = rec.source_kind

    # raw_mpn for display: prefer the longest (most-specific) display form seen.
    display, _ = _longest_nonempty([(r.raw_mpn or "", r.source_kind) for r in records])

    return ConsolidatedPart(
        normalized_mpn=records[0].normalized_mpn,
        raw_mpn=display or records[0].raw_mpn,
        manufacturer=mfr,
        description=desc,
        condition=cond,
        quantity=quantity,
        category=cat,
        specs=merged_specs,
        field_sources=field_sources,
        record_count=len(records),
    )


def consolidate(records: Iterable[SourceRecord]) -> list[ConsolidatedPart]:
    """Group cleaned records by ``normalized_mpn`` and consolidate each group.

    Insertion order of first appearance is preserved in the output. Returns one
    ConsolidatedPart per distinct ``normalized_mpn``. Records with an empty
    ``normalized_mpn`` are records that never went through clean_record (a pipeline
    wiring bug — parsers leave the dedup key blank): they are skipped, COUNTED, and
    surfaced as a WARNING so a mis-wired parse→consolidate shortcut cannot silently
    produce an empty/shrunken ingest.
    """
    groups: dict[str, list[SourceRecord]] = {}
    skipped_uncleaned = 0
    for rec in records:
        if not rec.normalized_mpn:
            skipped_uncleaned += 1
            continue
        groups.setdefault(rec.normalized_mpn, []).append(rec)
    if skipped_uncleaned:
        logger.warning(
            "consolidate: skipped {} record(s) with empty normalized_mpn — they bypassed "
            "clean_record (pipeline must be parse → clean → consolidate)",
            skipped_uncleaned,
        )
    return [_consolidate_group(group) for group in groups.values()]
