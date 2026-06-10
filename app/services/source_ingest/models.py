"""SP-Ingest dataclasses — the in-memory shapes that flow through the pipeline.

What: ``SourceRecord`` is one row as parsed/cleaned from a single TRIO source file (one
      part-occurrence). ``ConsolidatedPart`` is the merge of every SourceRecord sharing a
      ``normalized_mpn``, with per-field provenance (which source file/kind won each field)
      plus optional AI-correction fields filled by ai_correct.py.
Called by: parsers.py (emits SourceRecord), clean.py (cleans SourceRecord), consolidate.py
      (emits ConsolidatedPart), ai_correct.py (annotates ConsolidatedPart), ingest.py (reads
      ConsolidatedPart).
Depends on: stdlib dataclasses only — no app imports (keeps the data shapes import-cheap and
      testable in isolation).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Source-kind tags. A SourceRecord carries one of these so consolidate.py can apply the
# sfdc_master > inventory_sheet priority without re-deriving it from the file name.
SOURCE_KIND_SFDC_MASTER = "sfdc_master"
SOURCE_KIND_INVENTORY_SHEET = "inventory_sheet"

# Per-kind priority for consolidation tie-breaks (higher wins). The SFDC part master is the
# authoritative catalog; the operational sheets are per-unit copies.
SOURCE_KIND_PRIORITY: dict[str, int] = {
    SOURCE_KIND_SFDC_MASTER: 2,
    SOURCE_KIND_INVENTORY_SHEET: 1,
}

# Spec value scalars allowed in a SourceRecord/ConsolidatedPart spec dict.
SpecValue = str | int | float


@dataclass
class SourceRecord:
    """One part-occurrence parsed from a single TRIO source file.

    ``raw_mpn`` is the verbatim part number (suffixes intact); ``normalized_mpn`` is the
    dedup key (``normalize_mpn_key``) and is populated by clean.py — parsers leave it "".
    ``specs`` holds source-supplied deep facets keyed by app spec_key (only non-empty).
    """

    raw_mpn: str
    normalized_mpn: str = ""
    manufacturer: str | None = None
    description: str | None = None
    condition: str | None = None
    quantity: int | None = None
    category: str | None = None
    specs: dict[str, SpecValue] = field(default_factory=dict)
    source_file: str = ""
    source_kind: str = SOURCE_KIND_INVENTORY_SHEET


@dataclass
class ConsolidatedPart:
    """One MPN consolidated across every SourceRecord that shares its
    ``normalized_mpn``.

    ``field_sources`` records, per consolidated field, the ``source_kind`` that supplied the
    winning value (provenance). ``specs`` is the merged deep-facet dict (SFDC-master values
    win over sheet values). The ``ai_*`` fields are filled ONLY when ai_correct.py runs and
    are otherwise None/empty; ``ai_specs`` holds AI-extracted facets with confidences.
    """

    normalized_mpn: str
    raw_mpn: str
    manufacturer: str | None = None
    description: str | None = None
    condition: str | None = None
    quantity: int | None = None
    category: str | None = None
    specs: dict[str, SpecValue] = field(default_factory=dict)
    # Per-field provenance: field name -> source_kind that won it.
    field_sources: dict[str, str] = field(default_factory=dict)
    record_count: int = 0

    # --- AI-correction outputs (ai_correct.py) — None/empty until --ai-correct runs ---
    ai_description: str | None = None
    ai_category: str | None = None  # canonical key inferred when source category was missing
    ai_category_confidence: float | None = None
    # {spec_key: {"value": ..., "confidence": float}} — AI-extracted from the description text.
    ai_specs: dict[str, dict] = field(default_factory=dict)
