"""One-shot demand-telemetry backfill from the SFDC Weekly Export part master.

Usage: python -m app.management.import_demand_telemetry [--apply]
       [--csv /root/source_ingest/LSC1__Material__c.csv] [--chunk-size 5000]

DRY-RUN by DEFAULT (no writes — pass --apply to write). STREAMS the multi-hundred-MB
LSC1__Material__c.csv row-by-row (csv.DictReader, never wholly in memory), extracts
``Sourced_Qty_Last_90_Days__c`` (92.2% filled) + ``Most_Recent_Source_TS__c`` (87.9%),
skips IsDeleted rows (the ingest never created cards for them), and aggregates per
``normalize_mpn_key`` — the SAME dedup-key normalization source_ingest/clean.py used to
mint ``material_cards.normalized_mpn``, so matching is exact. Duplicate raw MPNs that
collapse to one key take the column-wise MAX (highest demand, latest sourcing event) —
order-independent and idempotent on re-runs.

Matched cards are bulk-updated in chunks (indexed ``normalized_mpn`` lookup +
executemany UPDATE of ``sourced_qty_90d`` / ``last_sourced_at``, commit per chunk) with
progress logging. Soft-deleted cards are skipped. These columns are a PRIORITIZATION
signal only (worker select_batch + spec-pass ordering, migration 105) — never a
displayed fact, so no F1-ladder arbitration applies (they are not provenanced
category/spec columns).

ONE-SHOT by design — NO cron, NO scheduler hook: the export is a static snapshot
(never refreshes on its own); re-running this command is the explicit operator step
whenever a NEW SFDC export actually lands.

Called by: an operator (manually, post-deploy of migration 105).
Depends on: app.database.SessionLocal, app.models.MaterialCard,
    app.services.source_ingest.parsers (_detect_text_encoding, _is_truthy),
    app.utils.normalization.normalize_mpn_key.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import bindparam, select, update
from sqlalchemy.orm import Session

from app.models import MaterialCard
from app.services.source_ingest.parsers import _detect_text_encoding, _is_truthy
from app.utils.normalization import normalize_mpn_key

DEFAULT_CSV = "/root/source_ingest/LSC1__Material__c.csv"

_MPN_COL = "LSC1__Material_Number__c"
_QTY_COL = "Sourced_Qty_Last_90_Days__c"
_TS_COL = "Most_Recent_Source_TS__c"

# SFDC report exports carry US-style naive timestamps ("2/5/2020 17:24"); occasional
# seconds / date-only variants are tolerated. Naive values are stamped UTC — the column
# is a coarse recency signal, so a few hours of timezone skew is immaterial.
_TS_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


# material_cards.sourced_qty_90d is Postgres INT4 (max 2,147,483,647). The column is a
# coarse PRIORITIZATION rank, never a displayed count, so we CLAMP out-of-range source
# values instead of widening the column: a few SFDC rows carry absurd 90-day quantities
# (e.g. manufacturer-name rows at 37e9 that aren't real parts) which would overflow INT4
# and, if stored via BIGINT, would dominate the ranking with junk. A clamped value still
# sorts at the top, which is the correct relative outcome.
_SOURCED_QTY_MAX = 2_147_483_647


def _parse_qty(raw: str | None) -> int | None:
    """Parse the sourced-qty cell ("14", "14.0", "") to an int, None when absent/junk.

    Clamps to ``_SOURCED_QTY_MAX`` so out-of-range SFDC artifacts can't overflow the
    INT4 column (the value is a ranking signal, so a clamped max still sorts first).
    """
    if raw is None or not raw.strip():
        return None
    try:
        return min(int(float(raw.strip())), _SOURCED_QTY_MAX)
    except ValueError:
        return None


def _parse_ts(raw: str | None) -> datetime | None:
    """Parse the most-recent-source timestamp cell to an aware UTC datetime, or None."""
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _max_present(a, b):
    """Column-wise max that treats None as "no value": the other operand wins, or the
    larger of the two when both are present."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def read_telemetry(csv_path: str | Path) -> tuple[dict[str, tuple[int | None, datetime | None]], dict]:
    """Stream the export and aggregate (qty, ts) per normalized MPN key.

    Returns ``(telemetry, stats)``. Only rows carrying at least one signal value enter
    the map (NULL-only rows would bloat it ~28k entries for zero ordering effect).
    Duplicate keys take the column-wise max. The aggregation map holds at most one
    small tuple per distinct MPN (~715k keys) — the 184-column rows themselves are
    never accumulated.
    """
    csv_path = Path(csv_path)
    telemetry: dict[str, tuple[int | None, datetime | None]] = {}
    stats = {"csv_rows": 0, "skipped_deleted": 0, "rows_with_signal": 0, "unparseable_ts": 0}
    with open(csv_path, encoding=_detect_text_encoding(csv_path), newline="") as fh:
        for row in csv.DictReader(fh):
            stats["csv_rows"] += 1
            if stats["csv_rows"] % 100_000 == 0:
                logger.info("import_demand_telemetry: streamed {} rows…", stats["csv_rows"])
            if _is_truthy(row.get("IsDeleted")):
                stats["skipped_deleted"] += 1
                continue
            key = normalize_mpn_key(row.get(_MPN_COL))
            if not key:
                continue
            qty = _parse_qty(row.get(_QTY_COL))
            raw_ts = row.get(_TS_COL)
            ts = _parse_ts(raw_ts)
            if ts is None and raw_ts and raw_ts.strip():
                stats["unparseable_ts"] += 1
            if qty is None and ts is None:
                continue
            stats["rows_with_signal"] += 1
            prev = telemetry.get(key)
            if prev is not None:
                prev_qty, prev_ts = prev
                qty = _max_present(qty, prev_qty)
                ts = _max_present(ts, prev_ts)
            telemetry[key] = (qty, ts)
    return telemetry, stats


def apply_telemetry(
    db: Session,
    telemetry: dict[str, tuple[int | None, datetime | None]],
    *,
    apply: bool,
    chunk_size: int = 5000,
) -> dict:
    """Match telemetry keys against live cards and bulk-update in chunks.

    Dry-run counts matches without writing. Apply mode runs an executemany UPDATE per
    chunk (indexed ``normalized_mpn`` equality; soft-deleted cards excluded) and
    commits per chunk so a mid-run failure keeps every completed chunk.
    """
    stats = {"distinct_keys": len(telemetry), "matched_cards": 0, "updated": 0}
    keys = list(telemetry.keys())
    stmt = (
        update(MaterialCard.__table__)
        .where(
            MaterialCard.__table__.c.normalized_mpn == bindparam("b_mpn"),
            MaterialCard.__table__.c.deleted_at.is_(None),
        )
        .values(sourced_qty_90d=bindparam("b_qty"), last_sourced_at=bindparam("b_ts"))
    )
    for start in range(0, len(keys), chunk_size):
        chunk = keys[start : start + chunk_size]
        matched = [
            row[0]
            for row in db.execute(
                select(MaterialCard.__table__.c.normalized_mpn).where(
                    MaterialCard.__table__.c.normalized_mpn.in_(chunk),
                    MaterialCard.__table__.c.deleted_at.is_(None),
                )
            )
        ]
        stats["matched_cards"] += len(matched)
        if apply and matched:
            params = [{"b_mpn": k, "b_qty": telemetry[k][0], "b_ts": telemetry[k][1]} for k in matched]
            db.execute(stmt, params)
            db.commit()
            stats["updated"] += len(matched)
        if (start // chunk_size) % 10 == 0:
            logger.info(
                "import_demand_telemetry: {}/{} keys processed — matched {} so far",
                min(start + chunk_size, len(keys)),
                len(keys),
                stats["matched_cards"],
            )
    return stats


def run_import(db: Session, *, csv_path: str | Path, apply: bool, chunk_size: int = 5000) -> dict:
    """Stream + aggregate + (dry-run|apply).

    Returns the combined stats dict.
    """
    mode = "APPLY" if apply else "DRY-RUN (no writes — pass --apply to write)"
    logger.info("import_demand_telemetry: starting in {} mode over {}", mode, csv_path)
    telemetry, read_stats = read_telemetry(csv_path)
    write_stats = apply_telemetry(db, telemetry, apply=apply, chunk_size=chunk_size)
    stats = {"apply": apply, **read_stats, **write_stats}
    logger.info("import_demand_telemetry [{}]: {}", mode, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Backfill demand-telemetry columns (dry-run by default).")
    parser.add_argument("--apply", action="store_true", help="Write the backfill (default: dry-run, no writes)")
    parser.add_argument("--csv", default=DEFAULT_CSV, help=f"Path to the LSC1 export (default: {DEFAULT_CSV})")
    parser.add_argument("--chunk-size", type=int, default=5000, help="Cards per match/update chunk")
    args = parser.parse_args(argv)

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        run_import(db, csv_path=args.csv, apply=args.apply, chunk_size=args.chunk_size)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
