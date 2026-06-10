"""SP-Ingest CLI — ingest TRIO source files into material_cards via the SP2 ladder.

What: Orchestrates parse → clean → consolidate → (ai_correct if --ai-correct) → ingest and
      prints a human report (source rows, distinct MPNs, would-create vs would-update, fields
      filled by source/tier, and ~15 sample consolidated parts). DRY RUN by default; pass
      --apply to write. Streams the ~620 MB ``LSC1__Material__c.csv`` part master; a
      ``LSC1__Manufacturers__c.csv`` in the same glob is auto-detected and used to resolve
      the master's manufacturer lookup IDs to names (never emitted raw).
Usage:
      python -m app.management.ingest_source_data [--files GLOB] [--ai-correct] [--apply] [--limit N]
Called by: an operator (manually). Depends on: app.database.SessionLocal, the source_ingest
      package (parsers/clean/consolidate/ai_correct/ingest), and Loguru. Per CLAUDE.md, services
      never print() — only this CLI prints its report.
"""

from __future__ import annotations

import argparse
import asyncio
import glob as globmod
from pathlib import Path

from loguru import logger

DEFAULT_GLOB = "/root/source_ingest/*"
# File-name fragments that mark the SFDC part master (streamed differently from sheets).
_SFDC_NAME_HINTS = ("material__c", "lsc1__material")
# File-name fragment that marks the SFDC manufacturers lookup (resolves the part master's
# LSC1__Manufacturer_Brand__c Salesforce IDs → names; CATALOG.md "manufacturer-lookup").
_MANUFACTURERS_NAME_HINT = "manufacturers__c"
# Suffixes we know how to parse as operational sheets.
_SHEET_SUFFIXES = (".csv", ".xlsx", ".xlsm", ".txt")


def _is_sfdc_master(path: Path) -> bool:
    return any(hint in path.name.lower() for hint in _SFDC_NAME_HINTS)


def _is_manufacturers_lookup(path: Path) -> bool:
    return _MANUFACTURERS_NAME_HINT in path.name.lower()


def _discover_files(pattern: str) -> tuple[list[Path], Path | None]:
    """Expand the glob to (parseable source files, manufacturers-lookup CSV or None).

    Skips docs/markdown/binaries; the manufacturers CSV is returned separately — it is a
    lookup table, not a part source.
    """
    paths = []
    manufacturers: Path | None = None
    for raw in sorted(globmod.glob(pattern)):
        p = Path(raw)
        if not p.is_file():
            continue
        if _is_manufacturers_lookup(p) and p.suffix.lower() == ".csv":
            manufacturers = p
        elif _is_sfdc_master(p) and p.suffix.lower() == ".csv":
            paths.append(p)
        elif p.suffix.lower() in _SHEET_SUFFIXES and not p.name.lower().endswith(".md"):
            paths.append(p)
    return paths, manufacturers


def _parse_all(files: list[Path], limit: int | None, manufacturer_lookup: dict[str, str] | None):
    """Yield raw SourceRecords across all files (SFDC master streamed, others as
    sheets)."""
    from app.services.source_ingest.parsers import (
        parse_inventory_sheet,
        parse_sfdc_material_master,
    )

    emitted = 0
    for path in files:
        if _is_sfdc_master(path):
            rows = parse_sfdc_material_master(path, manufacturer_lookup)
        else:
            rows = parse_inventory_sheet(path)
        for rec in rows:
            yield rec
            emitted += 1
            if limit is not None and emitted >= limit:
                logger.info("Reached --limit {} raw rows; stopping parse", limit)
                return


async def run(*, pattern: str, ai_correct_flag: bool, apply: bool, limit: int | None) -> dict:
    """Run the full pipeline and return a report dict (parsing → clean → consolidate →
    ingest)."""
    from app.database import SessionLocal
    from app.services.source_ingest.clean import clean_record
    from app.services.source_ingest.consolidate import consolidate

    files, manufacturers_path = _discover_files(pattern)
    logger.info(
        "SP-Ingest: {} source file(s) matched {!r} (manufacturers lookup: {})",
        len(files),
        pattern,
        manufacturers_path.name if manufacturers_path else "none",
    )

    manufacturer_lookup: dict[str, str] | None = None
    if manufacturers_path is not None:
        from app.services.source_ingest.parsers import parse_sfdc_manufacturers

        manufacturer_lookup = parse_sfdc_manufacturers(manufacturers_path)

    raw_count = 0
    cleaned = []
    for rec in _parse_all(files, limit, manufacturer_lookup):
        raw_count += 1
        c = clean_record(rec)
        if c is not None:
            cleaned.append(c)

    parts = consolidate(cleaned)
    logger.info("Cleaned {}/{} rows → {} distinct MPNs", len(cleaned), raw_count, len(parts))

    if ai_correct_flag:
        from app.services.source_ingest.ai_correct import ai_correct

        logger.info("AI-correcting {} parts (smart tier)…", len(parts))
        parts = await ai_correct(parts)

    db = SessionLocal()
    try:
        from app.services.source_ingest.ingest import ingest

        stats = ingest(db, parts, apply=apply)
    finally:
        db.close()

    return {
        "files": [f.name for f in files],
        "raw_rows": raw_count,
        "cleaned_rows": len(cleaned),
        "distinct_mpns": len(parts),
        "ai_correct": ai_correct_flag,
        "apply": apply,
        "stats": stats,
    }


def _print_report(report: dict) -> None:
    """Print the human-readable pipeline report.

    (CLI may print; services may not.)
    """
    s = report["stats"]
    mode = "APPLY (writes committed)" if report["apply"] else "DRY RUN (no writes)"
    print("\n" + "=" * 72)
    print(f"  SP-Ingest report — {mode}")
    print("=" * 72)
    print(f"  Source files       : {', '.join(report['files']) or '(none matched)'}")
    print(f"  Raw source rows    : {report['raw_rows']}")
    print(f"  Cleaned rows       : {report['cleaned_rows']}")
    print(f"  Distinct MPNs      : {report['distinct_mpns']}")
    print(f"  AI correction      : {'on' if report['ai_correct'] else 'off'}")
    print("-" * 72)
    if report["apply"]:
        print(f"  Cards created      : {s['created']}")
        print(f"  Cards updated      : {s['updated']}")
    else:
        print(f"  Would create       : {s['would_create']}")
        print(f"  Would update       : {s['would_update']}")
    print(f"  Categories set     : {s['categories_set']}")
    print(f"  Descriptions filled: {s['descriptions_filled']}")
    print(f"  Conditions filled  : {s['conditions_filled']}")
    print(f"  Specs written      : {s['specs_written']}")
    print("  Fields filled by source/tier:")
    for source, count in sorted(s["fields_by_source"].items()):
        print(f"      {source:<16}: {count}")
    print("-" * 72)
    print(f"  Sample consolidated parts (up to {len(s['sample'])}):")
    for row in s["sample"]:
        print(
            f"    [{row['action']}] {row['display_mpn']}  cat={row['category']}"
            f" ({row['category_source']})  cond={row['condition']}  specs={list(row['specs'])}"
            + (f"  ai_specs={list(row['ai_specs'])}" if row["ai_specs"] else "")
        )
        if row["description"]:
            print(f"        {row['description']}")
    print("=" * 72 + "\n")


def main(argv: list[str] | None = None) -> dict:
    """Parse args, run the pipeline, print the report.

    Returns the report (for tests).
    """
    parser = argparse.ArgumentParser(description="Ingest TRIO source data into material_cards.")
    parser.add_argument("--files", default=DEFAULT_GLOB, help="Glob of source files (default: %(default)s)")
    parser.add_argument("--ai-correct", action="store_true", help="Run Claude AI correction (smart tier)")
    parser.add_argument("--apply", action="store_true", help="Write to the DB (default: dry run)")
    parser.add_argument("--limit", type=int, default=None, help="Cap raw rows parsed (smoke-test aid)")
    args = parser.parse_args(argv)

    report = asyncio.run(run(pattern=args.files, ai_correct_flag=args.ai_correct, apply=args.apply, limit=args.limit))
    _print_report(report)
    return report


if __name__ == "__main__":
    main()
