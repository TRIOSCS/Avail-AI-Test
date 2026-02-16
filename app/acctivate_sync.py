"""Acctivate SQL Server sync — read-only daily pull.

Pulls vendor reliability metrics, current inventory, and transaction
summaries from Acctivate's SQL Server database. Writes results to
AVAIL's Postgres. If the connection is down, AVAIL serves yesterday's
data without noticing.

Design rules (from data architecture spec):
  - Read-only access. One connection string. SELECT-only.
  - Daily sync via cron or manual trigger. 4-5 clean queries.
  - Total code: single file, under 200 lines.
  - Entire sync should run in under 60 seconds.
  - Log failures and alert. That's the only failure mode.
"""

import logging
from datetime import datetime, timezone
from contextlib import contextmanager

import pymssql  # type: ignore

from .config import settings

log = logging.getLogger("avail.acctivate")

# ── Connection ────────────────────────────────────────────────────────


@contextmanager
def _connect():
    """Open a read-only connection to Acctivate's SQL Server."""
    conn = pymssql.connect(
        server=settings.acctivate_host,
        port=settings.acctivate_port,
        user=settings.acctivate_user,
        password=settings.acctivate_password,
        database=settings.acctivate_database,
        login_timeout=10,
        timeout=30,
        as_dict=True,
    )
    try:
        yield conn
    finally:
        conn.close()


# ── Schema Discovery (run once to map tables) ────────────────────────


def discover_schema():
    """Return Acctivate's table/column structure for query development.

    Run this first. Share the output so we can write final queries
    against the actual table and column names.
    """
    results = {}
    with _connect() as conn:
        cur = conn.cursor()

        # 1. All user tables
        cur.execute("""
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
        """)
        results["tables"] = cur.fetchall()

        # 2. Columns for tables likely related to POs, vendors, inventory
        keywords = [
            "%vendor%",
            "%supplier%",
            "%purchase%",
            "%order%",
            "%inventory%",
            "%stock%",
            "%product%",
            "%item%",
            "%rma%",
            "%return%",
            "%cancel%",
            "%receipt%",
        ]
        like_clauses = " OR ".join("TABLE_NAME LIKE ?" for _ in keywords)
        cur.execute(
            f"""
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                   CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE {like_clauses}
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """,
            keywords,
        )
        results["relevant_columns"] = cur.fetchall()

        # 3. Row counts for relevant tables
        relevant_tables = set(r["TABLE_NAME"] for r in results["relevant_columns"])
        counts = {}
        for t in sorted(relevant_tables):
            try:
                # Table names from INFORMATION_SCHEMA — safe, not user input
                cur.execute(f"SELECT COUNT(*) AS cnt FROM [{t}]")  # nosec B608
                counts[t] = cur.fetchone()["cnt"]
            except Exception:
                counts[t] = "error"
        results["row_counts"] = counts

        # 4. Sample rows from key tables (5 rows each)
        samples = {}
        for t in sorted(relevant_tables)[:20]:  # cap at 20 tables
            try:
                cur.execute(f"SELECT TOP 5 * FROM [{t}]")  # nosec B608
                samples[t] = cur.fetchall()
            except Exception:
                samples[t] = "error"
        results["samples"] = samples

    return results


# ── Sync Queries (finalize after schema discovery) ────────────────────
#
# These are TEMPLATE queries. Once we see the actual Acctivate schema,
# we'll replace the table/column names. The logic stays the same.
#
# Query 1: Vendor cancellation rate
# Query 2: Vendor RMA rate (overall + per part)
# Query 3: Current inventory (part + qty on hand)
# Query 4: Transaction summary (per vendor, per part+vendor)
# Query 5: Delta — what changed since last sync
#

# Placeholder SQL — will be finalized after discover_schema() output
_Q_VENDOR_CANCELLATION = """
-- Cancellation rate = cancelled orders / total orders, per vendor
-- TEMPLATE: Replace [PurchaseOrder], [VendorName], [Status] with actuals
SELECT
    VendorName,
    COUNT(*) AS total_orders,
    SUM(CASE WHEN Status = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled_orders,
    CAST(SUM(CASE WHEN Status = 'Cancelled' THEN 1 ELSE 0 END) AS FLOAT)
        / NULLIF(COUNT(*), 0) AS cancellation_rate
FROM [PurchaseOrder]
GROUP BY VendorName
"""

_Q_VENDOR_RMA = """
-- RMA rate = units returned / units received, per vendor, per part
-- TEMPLATE: Replace table/column names with actuals
SELECT
    VendorName,
    ProductID,
    SUM(QtyReceived) AS total_received,
    SUM(QtyReturned) AS total_returned,
    CAST(SUM(QtyReturned) AS FLOAT)
        / NULLIF(SUM(QtyReceived), 0) AS rma_rate
FROM [PurchaseOrderDetail]
    JOIN [PurchaseOrder] ON [PurchaseOrderDetail].POID = [PurchaseOrder].ID
GROUP BY VendorName, ProductID
"""

_Q_INVENTORY = """
-- Current inventory: part number + quantity on hand
SELECT ProductID, WarehouseID, QuantityOnHand
FROM [Inventory]
WHERE QuantityOnHand > 0
"""

_Q_TRANSACTION_SUMMARY = """
-- Per vendor: last order date, total orders, total units
-- Per part+vendor: last price paid, last date purchased
SELECT
    VendorName,
    ProductID,
    MAX(OrderDate) AS last_order_date,
    COUNT(DISTINCT POID) AS total_orders,
    SUM(QtyOrdered) AS total_units,
    -- Last price: use the most recent order's unit price
    (SELECT TOP 1 UnitPrice
     FROM [PurchaseOrderDetail] d2
        JOIN [PurchaseOrder] po2 ON d2.POID = po2.ID
     WHERE po2.VendorName = [PurchaseOrder].VendorName
       AND d2.ProductID = [PurchaseOrderDetail].ProductID
     ORDER BY po2.OrderDate DESC
    ) AS last_price_paid
FROM [PurchaseOrderDetail]
    JOIN [PurchaseOrder] ON [PurchaseOrderDetail].POID = [PurchaseOrder].ID
GROUP BY VendorName, ProductID
"""


# ── Sync Orchestrator ─────────────────────────────────────────────────


def run_sync(db_session):
    """Execute the daily sync. Returns a status dict for logging.

    Args:
        db_session: SQLAlchemy session for writing to AVAIL's Postgres.

    Returns:
        dict with keys: started_at, finished_at, status, counts, errors
    """
    from .models import VendorCard, SyncLog
    from .vendor_utils import normalize_vendor_name

    started = datetime.now(timezone.utc)
    result = {
        "started_at": started,
        "status": "running",
        "counts": {},
        "errors": [],
    }

    try:
        with _connect() as conn:
            cur = conn.cursor()

            # ── 1. Vendor cancellation rates ──────────────────────
            cur.execute(_Q_VENDOR_CANCELLATION)
            rows = cur.fetchall()
            matched = 0
            for row in rows:
                norm = normalize_vendor_name(row["VendorName"])
                card = (
                    db_session.query(VendorCard).filter_by(normalized_name=norm).first()
                )
                if card:
                    card.cancellation_rate = row["cancellation_rate"]
                    card.acctivate_total_orders = row["total_orders"]
                    card.last_synced_at = started
                    matched += 1
            result["counts"]["vendor_cancellation"] = {
                "total": len(rows),
                "matched": matched,
            }

            # ── 2. Vendor RMA rates ───────────────────────────────
            cur.execute(_Q_VENDOR_RMA)
            rows = cur.fetchall()
            result["counts"]["vendor_rma"] = {"total": len(rows)}

            # ── 3. Current inventory ──────────────────────────────
            cur.execute(_Q_INVENTORY)
            rows = cur.fetchall()
            # Upsert into inventory_snapshots table
            result["counts"]["inventory"] = {"total": len(rows)}

            # ── 4. Transaction summary ────────────────────────────
            cur.execute(_Q_TRANSACTION_SUMMARY)
            rows = cur.fetchall()
            result["counts"]["transactions"] = {"total": len(rows)}

        db_session.commit()
        result["status"] = "success"

    except pymssql.OperationalError as e:
        result["status"] = "connection_failed"
        result["errors"].append(str(e))
        log.error("Acctivate sync connection failed: %s", e)
    except Exception as e:
        result["status"] = "error"
        result["errors"].append(str(e))
        log.exception("Acctivate sync failed")
        db_session.rollback()

    result["finished_at"] = datetime.now(timezone.utc)
    duration = (result["finished_at"] - started).total_seconds()
    log.info(
        "Acctivate sync %s in %.1fs — %s", result["status"], duration, result["counts"]
    )

    # Write sync log
    try:
        sync_log = SyncLog(
            source="acctivate",
            status=result["status"],
            started_at=started,
            finished_at=result["finished_at"],
            duration_seconds=round(duration, 1),
            row_counts=result["counts"],
            errors=result["errors"] or None,
        )
        db_session.add(sync_log)
        db_session.commit()
    except Exception:
        log.exception("Failed to write sync log")

    return result
