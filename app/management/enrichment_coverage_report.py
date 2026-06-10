"""Daily enrichment-coverage telemetry report (read-only).

What: Aggregates material-card enrichment coverage into one compact human-readable
      block (or a structured dict with --json): card/category coverage, facet-table
      coverage per commodity, specs_structured source mix, category provenance
      (category_source counts incl. the "(none)" no-provenance bucket), facet
      provenance (facet source counts incl. "(none)" for rows the 096 backfill
      could not match), an unregistered-source callout (any observed source string
      missing from spec_tiers.SOURCE_TIER ranks at tier 0 and loses every conflict),
      enrichment_status distribution, description coverage, and fru_links totals.
      With --log-file it appends a JSONL history line ({ts, metrics}) and prints
      run-over-run deltas for the headline numbers.
Usage: python -m app.management.enrichment_coverage_report [--json] [--log-file PATH]
Called by: admin manually; daily ops cron via scripts/enrichment_coverage_cron.sh
      (host crontab — see that script's header for the install line).
Depends on: MaterialCard, MaterialSpecFacet, FruLink models; spec_tiers.SOURCE_TIER
      (the unregistered-source cross-check); app.database.SessionLocal
      (single read-only session — performs no writes; on PostgreSQL all queries share
      one REPEATABLE READ snapshot so concurrent enrichment-worker writes cannot skew
      cross-metric ratios).
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import and_, case, func, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from app.models import MaterialCard, MaterialSpecFacet
from app.models.fru_link import FruLink
from app.services.spec_tiers import SOURCE_TIER

TOP_N = 15

# Headline numbers for run-over-run deltas: (dotted path into metrics, display label).
HEADLINES: tuple[tuple[str, str], ...] = (
    ("cards.total", "cards"),
    ("cards.with_category", "with-category"),
    ("cards.with_description", "with-description"),
    ("facets.cards_with_facets", "faceted-cards"),
    ("facets.rows_total", "facet-rows"),
    ("spec_entries_total", "spec-entries"),
    ("fru_links.rows", "fru-rows"),
)

# specs_structured source counting. PG iterates the JSONB in SQL (one query);
# SQLite uses the equivalent json_each; any other dialect falls back to one
# streamed Python pass. The jsonb_typeof/json_type guards skip legacy non-object
# payloads, and non-dict entry values count under "(none)". Keep all three
# branches aligned: only a MISSING or JSON-null source maps to "(none)" — a
# present-but-empty source ("") is its own bucket. Non-string scalar sources
# render as JSON text in PG (->>) and the Python fallback ('true' / '0');
# SQLite's json_extract renders booleans as 1/0 — sources are strings in
# practice, so that residual difference is accepted.
_PG_SOURCES_SQL = text(
    """
    SELECT COALESCE(e.value ->> 'source', '(none)') AS src, count(*) AS n
    FROM (
        SELECT specs_structured FROM material_cards
        WHERE deleted_at IS NULL
          AND specs_structured IS NOT NULL
          AND jsonb_typeof(specs_structured) = 'object'
    ) c
    CROSS JOIN LATERAL jsonb_each(c.specs_structured) AS e
    GROUP BY src
    ORDER BY n DESC, src
    """
)
_SQLITE_SOURCES_SQL = text(
    """
    SELECT COALESCE(
               CASE WHEN j.type = 'object' THEN json_extract(j.value, '$.source') END,
               '(none)'
           ) AS src,
           count(*) AS n
    FROM material_cards c, json_each(c.specs_structured) AS j
    WHERE c.deleted_at IS NULL
      AND c.specs_structured IS NOT NULL
      AND json_type(c.specs_structured) = 'object'
    GROUP BY src
    ORDER BY n DESC, src
    """
)


def _pct(part: int, total: int) -> float:
    return round(100.0 * part / total, 1) if total else 0.0


def _source_bucket(src: object) -> str:
    """NULL provenance buckets as "(none)" — same convention as the spec-source
    counter."""
    return str(src) if src is not None else "(none)"


def _spec_source_counts(db: Session) -> dict[str, int]:
    """Count specs_structured entries per recorded source, descending."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        rows = db.execute(_PG_SOURCES_SQL).all()
    elif dialect == "sqlite":
        rows = db.execute(_SQLITE_SOURCES_SQL).all()
    else:  # portable fallback — one streamed query, counted in Python
        counts: Counter[str] = Counter()
        query = db.query(MaterialCard.specs_structured).filter(
            MaterialCard.deleted_at.is_(None), MaterialCard.specs_structured.isnot(None)
        )
        for (specs,) in query.yield_per(1000):
            if not isinstance(specs, dict):
                continue
            for entry in specs.values():
                source = entry.get("source") if isinstance(entry, dict) else None
                if source is None:
                    key = "(none)"
                elif isinstance(source, str):
                    key = source
                else:
                    key = json.dumps(source)  # JSON text, like PG's ->> ('true', '0')
                counts[key] += 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {str(src): int(n) for src, n in rows}


def _pin_snapshot(db: Session) -> None:
    """Pin one consistent snapshot for the whole metric collection (PostgreSQL).

    At PG's default READ COMMITTED every statement sees its own snapshot, so a
    concurrently writing enrichment worker could skew derived ratios (e.g. a faceted-
    cards numerator over a cards total taken statements earlier). REPEATABLE READ makes
    all queries in this transaction read one snapshot; the session stays read-only. Must
    run before the transaction's first statement, so callers should pass a fresh session
    (main() does). SQLite is already snapshot-consistent per transaction; other dialects
    keep their default.
    """
    if db.get_bind().dialect.name == "postgresql" and not db.in_transaction():
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})


def collect_metrics(db: Session) -> dict[str, Any]:
    """Gather all coverage metrics with a handful of read-only aggregate queries.

    On PostgreSQL the queries share one REPEATABLE READ snapshot (see _pin_snapshot) so
    the figures are mutually consistent even while the enrichment worker writes.
    """
    _pin_snapshot(db)
    active = MaterialCard.deleted_at.is_(None)
    category_norm = func.lower(func.trim(MaterialCard.category))
    has_category = and_(MaterialCard.category.isnot(None), func.trim(MaterialCard.category) != "")
    has_description = and_(MaterialCard.description.isnot(None), func.trim(MaterialCard.description) != "")

    # 1. Cards: totals + category/description coverage in one aggregate query.
    total, with_category, category_other, with_description = (
        db.query(
            func.count(MaterialCard.id),
            func.count(case((has_category, 1))),
            func.count(case((category_norm == "other", 1))),
            func.count(case((has_description, 1))),
        )
        .filter(active)
        .one()
    )
    top_categories = (
        db.query(category_norm, func.count(MaterialCard.id))
        .filter(active, has_category)
        .group_by(category_norm)
        .order_by(func.count(MaterialCard.id).desc(), category_norm)
        .limit(TOP_N)
        .all()
    )

    # 2. Facets: coverage + per-commodity rows/spec-keys (facet.category = commodity).
    facet_join = MaterialCard.id == MaterialSpecFacet.material_card_id
    rows_total, cards_with_facets = (
        db.query(
            func.count(MaterialSpecFacet.id),
            func.count(MaterialSpecFacet.material_card_id.distinct()),
        )
        .join(MaterialCard, facet_join)
        .filter(active)
        .one()
    )
    by_commodity = (
        db.query(
            MaterialSpecFacet.category,
            func.count(MaterialSpecFacet.id),
            func.count(MaterialSpecFacet.spec_key.distinct()),
        )
        .join(MaterialCard, facet_join)
        .filter(active)
        .group_by(MaterialSpecFacet.category)
        .order_by(func.count(MaterialSpecFacet.id).desc(), MaterialSpecFacet.category)
        .limit(TOP_N)
        .all()
    )

    # 3. Sources: specs_structured entries per recorded source.
    spec_sources = _spec_source_counts(db)

    # 3b. Category provenance: categorized cards per category_source. Spec-entry counts
    # alone are WINS-only and spec-only — a card categorized by an ingest that wrote no
    # specs (or whose spec writes all lost the ladder) is invisible in spec_sources, so
    # "trio_source: 0 spec entries" after an ingest does NOT mean the ingest wrote
    # nothing. "(none)" = categorized rows with NULL category_source (a writer
    # bypassing set_category, or pre-096 data the backfill should have stamped).
    category_source_rows = (
        db.query(MaterialCard.category_source, func.count(MaterialCard.id))
        .filter(active, has_category)
        .group_by(MaterialCard.category_source)
        .order_by(func.count(MaterialCard.id).desc())
        .all()
    )
    category_sources = {_source_bucket(src): int(n) for src, n in category_source_rows}

    # 3c. Facet provenance: facet rows per source. "(none)" = rows with NULL provenance —
    # the 096 backfill only stamped rows whose spec_key still existed in the card's
    # specs_structured JSONB, so orphans stay NULL and would otherwise be invisible.
    facet_source_rows = (
        db.query(MaterialSpecFacet.source, func.count(MaterialSpecFacet.id))
        .join(MaterialCard, facet_join)
        .filter(active)
        .group_by(MaterialSpecFacet.source)
        .order_by(func.count(MaterialSpecFacet.id).desc())
        .all()
    )
    facet_sources = {_source_bucket(src): int(n) for src, n in facet_source_rows}

    # 3d. Unregistered-source callout: any observed source string missing from the F1
    # ladder maps to tier 0 (spec_tiers.tier_for) and loses EVERY conflict — a
    # misregistered writer is otherwise visible only as a once-per-process WARNING
    # plus DEBUG per-row logs. Surfacing it here makes it trend in the daily report.
    observed = set(spec_sources) | set(category_sources) | set(facet_sources)
    unregistered_sources = sorted(observed - set(SOURCE_TIER) - {"(none)"})

    # 4. enrichment_status distribution.
    status_rows = (
        db.query(MaterialCard.enrichment_status, func.count(MaterialCard.id))
        .filter(active)
        .group_by(MaterialCard.enrichment_status)
        .order_by(func.count(MaterialCard.id).desc(), MaterialCard.enrichment_status)
        .all()
    )

    # 5. fru_links totals — only if the table exists in this database. Inspect the
    # session's own connection (NOT the engine, which would check on a second
    # pooled connection outside this transaction's snapshot).
    fru_links: dict[str, int] | None = None
    if sa_inspect(db.connection()).has_table(FruLink.__tablename__):
        fru_rows, fru_distinct = db.query(func.count(FruLink.id), func.count(FruLink.fru_norm.distinct())).one()
        fru_links = {"rows": int(fru_rows), "distinct_frus": int(fru_distinct)}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cards": {
            "total": int(total),
            "with_category": int(with_category),
            "with_category_pct": _pct(int(with_category), int(total)),
            "category_other": int(category_other),
            "with_description": int(with_description),
            "top_categories": [{"category": str(cat), "count": int(n)} for cat, n in top_categories],
        },
        "facets": {
            "cards_with_facets": int(cards_with_facets),
            "cards_with_facets_pct": _pct(int(cards_with_facets), int(total)),
            "rows_total": int(rows_total),
            "by_commodity": [
                {"commodity": str(commodity), "rows": int(rows), "spec_keys": int(keys)}
                for commodity, rows, keys in by_commodity
            ],
        },
        "spec_sources": spec_sources,
        "spec_entries_total": sum(spec_sources.values()),
        "category_sources": category_sources,
        "facet_sources": facet_sources,
        "unregistered_sources": unregistered_sources,
        "enrichment_status": {str(status): int(n) for status, n in status_rows},
        "fru_links": fru_links,
    }


def _dig(metrics: dict[str, Any], path: str) -> Any:
    current: Any = metrics
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def compute_deltas(prev: dict[str, Any], curr: dict[str, Any]) -> dict[str, int]:
    """Headline-number deltas (curr - prev); keys missing on either side are skipped."""
    deltas: dict[str, int] = {}
    for path, _label in HEADLINES:
        a, b = _dig(prev, path), _dig(curr, path)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            deltas[path] = int(b) - int(a)
    return deltas


def read_last_metrics(path: Path) -> dict[str, Any] | None:
    """Return the metrics dict from the last well-formed JSONL line, if any.

    Scans backwards past unusable trailing lines — malformed JSON (e.g. a torn write
    from a crash mid-append) or a line without a dict "metrics" key (e.g. a hand edit) —
    logging a warning for each skipped line, so one bad line costs only its own
    datapoint, never the delta computation itself.
    """
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        lines = [line.strip() for line in fh if line.strip()]
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line in {} while reading last metrics entry", path)
            continue
        metrics = entry.get("metrics") if isinstance(entry, dict) else None
        if isinstance(metrics, dict):
            return metrics
        logger.warning("Skipping line without a 'metrics' dict in {} while reading last metrics entry", path)
    return None


def append_metrics(path: Path, metrics: dict[str, Any]) -> None:
    """Append one ``{ts, metrics}`` JSONL line.

    If a previous run crashed mid-write the file can end in a torn line with no trailing
    newline — heal that first so the new entry never merges into (and gets destroyed by)
    the corrupt line.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": metrics["generated_at"], "metrics": metrics}, default=str)
    with path.open("a+b") as fh:
        fh.seek(0, 2)
        if fh.tell() > 0:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                fh.write(b"\n")
        fh.write((line + "\n").encode("utf-8"))


def _join(pairs: list[tuple[str, str]]) -> str:
    # Blank labels (e.g. an empty-string spec source) render quoted so they stay visible.
    return " · ".join(f"{k if k.strip() else repr(k)} {v}" for k, v in pairs) if pairs else "(none)"


def format_report(metrics: dict[str, Any], deltas: dict[str, int] | None = None, prev_ts: str | None = None) -> str:
    """One compact human-readable block."""
    cards, facets = metrics["cards"], metrics["facets"]
    lines = [
        f"Enrichment coverage — {metrics['generated_at']}",
        (
            f"Cards: {cards['total']} total · category {cards['with_category']}"
            f" ({cards['with_category_pct']}%) · 'other' {cards['category_other']}"
            f" · description {cards['with_description']}"
        ),
        "  Top categories: " + _join([(t["category"], str(t["count"])) for t in cards["top_categories"]]),
        (
            f"Facets: {facets['cards_with_facets']} cards covered ({facets['cards_with_facets_pct']}%)"
            f" · {facets['rows_total']} rows"
        ),
        "  By commodity: "
        + _join([(c["commodity"], f"{c['rows']} rows/{c['spec_keys']} keys") for c in facets["by_commodity"]]),
        f"Spec sources ({metrics['spec_entries_total']} entries): "
        + _join([(k, str(v)) for k, v in metrics["spec_sources"].items()]),
        "Category sources: " + _join([(k, str(v)) for k, v in metrics["category_sources"].items()]),
        "Facet sources: " + _join([(k, str(v)) for k, v in metrics["facet_sources"].items()]),
        "Status: " + _join([(k, str(v)) for k, v in metrics["enrichment_status"].items()]),
    ]
    if metrics["unregistered_sources"]:
        lines.append(
            "WARNING unregistered sources (tier 0 — every write loses conflicts): "
            + ", ".join(metrics["unregistered_sources"])
        )
    fru = metrics["fru_links"]
    if fru is not None:
        lines.append(f"FRU links: {fru['rows']} rows · {fru['distinct_frus']} distinct FRUs")
    else:
        lines.append("FRU links: (table absent)")
    if deltas is not None:
        labels = dict(HEADLINES)
        parts = [f"{labels[path]} {delta:+d}" for path, delta in deltas.items()]
        suffix = f" (vs {prev_ts})" if prev_ts else ""
        lines.append(f"Δ since last run{suffix}: " + (" · ".join(parts) if parts else "(no comparable numbers)"))
    return "\n".join(lines)


def main(json_output: bool = False, log_file: str | None = None) -> dict[str, Any]:
    """Collect, optionally log + delta, and print the report.

    Returns the output dict.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        metrics = collect_metrics(db)
    finally:
        db.close()

    deltas: dict[str, int] | None = None
    prev_ts: str | None = None
    if log_file:
        path = Path(log_file)
        prev = read_last_metrics(path)
        if prev is not None:
            deltas = compute_deltas(prev, metrics)
            prev_ts = prev.get("generated_at")
        append_metrics(path, metrics)

    output = dict(metrics)
    if deltas is not None:
        output["deltas"] = deltas
    if json_output:
        print(json.dumps(output, indent=2, default=str))  # noqa: T201 — CLI report output
    else:
        print(format_report(metrics, deltas, prev_ts))  # noqa: T201 — CLI report output
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Daily enrichment-coverage report (read-only)")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit machine-readable JSON")
    parser.add_argument("--log-file", default=None, help="JSONL history file; enables run-over-run deltas")
    args = parser.parse_args()
    main(json_output=args.json_output, log_file=args.log_file)
