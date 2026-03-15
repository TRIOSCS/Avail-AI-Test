/**
 * intake_helpers.mjs — Pure helper functions for the free-form intake drawer.
 *
 * Handles row normalization, TSV fallback parsing, and duplicate detection
 * for bulk intake of requirements and offers.
 *
 * Called by: htmx_app.js intake drawer component
 * Depends on: nothing (pure functions)
 */

/**
 * Normalize a raw intake row: trim strings, apply defaults, set duplicate=false.
 */
export function normalizeIntakeRow(raw, mode) {
  const row = { ...raw };
  if (row.mpn) row.mpn = row.mpn.trim();
  if (row.vendor_name) row.vendor_name = row.vendor_name.trim();
  if (row.qty) row.qty = String(row.qty).trim();
  if (row.unit_price) row.unit_price = String(row.unit_price).trim();
  if (row.confidence) row.confidence = String(row.confidence).trim();
  if (!row.row_type) {
    row.row_type = mode === "offer" ? "offer" : "requirement";
  }
  row.duplicate = false;
  delete row.duplicate_reason;
  return row;
}

/**
 * Parse tab-separated text into an array of normalized intake rows.
 * First line is treated as a header if it matches known column names,
 * otherwise all lines are treated as data with positional columns (MPN, QTY, PRICE).
 */
export function parseFallbackRows(tsvText, mode, context) {
  const lines = tsvText.trim().split("\n");
  if (lines.length === 0) return [];

  const headerCols = lines[0].split("\t").map((c) => c.trim().toUpperCase());
  const knownHeaders = new Set(["MPN", "QTY", "PRICE", "VENDOR", "VENDOR_NAME"]);
  const hasHeader = headerCols.some((h) => knownHeaders.has(h));

  const dataLines = hasHeader ? lines.slice(1) : lines;
  const rowType = mode === "offer" ? "offer" : "requirement";

  return dataLines
    .filter((line) => line.trim())
    .map((line) => {
      const cols = line.split("\t").map((c) => c.trim());
      const raw = {
        row_type: rowType,
        mpn: cols[0] || "",
        qty: cols[1] || "",
        unit_price: cols[2] || "",
      };
      if (context && context.vendor_name) {
        raw.vendor_name = context.vendor_name;
      }
      return normalizeIntakeRow(raw, mode);
    });
}

/**
 * Build a deduplication key for offer rows: "VENDOR::MPN" (uppercased).
 */
export function offerDuplicateKey({ vendor_name, mpn }) {
  return `${(vendor_name || "").toUpperCase()}::${(mpn || "").toUpperCase()}`;
}

/**
 * Mark duplicate rows in-place. Checks for:
 * - Internal duplicates (same MPN for requirements, same vendor+MPN for offers)
 * - Existing data duplicates (against provided sets)
 */
export function applyDuplicateMarkers(rows, { existingRequirementMpns, existingOfferKeys }) {
  const seenReqMpns = new Set();
  const seenOfferKeys = new Set();

  for (const row of rows) {
    row.duplicate = false;
    delete row.duplicate_reason;

    if (row.row_type === "requirement") {
      const mpnUpper = (row.mpn || "").toUpperCase();
      if (existingRequirementMpns && existingRequirementMpns.has(mpnUpper)) {
        row.duplicate = true;
        row.duplicate_reason = "Duplicate RFQ line: already exists";
      } else if (seenReqMpns.has(mpnUpper)) {
        row.duplicate = true;
        row.duplicate_reason = "Duplicate RFQ line in this batch";
      } else {
        seenReqMpns.add(mpnUpper);
      }
    } else if (row.row_type === "offer") {
      const key = offerDuplicateKey(row);
      if (existingOfferKeys && existingOfferKeys.has(key)) {
        row.duplicate = true;
        row.duplicate_reason = "Duplicate offer: already exists";
      } else if (seenOfferKeys.has(key)) {
        row.duplicate = true;
        row.duplicate_reason = "Duplicate offer in this batch";
      } else {
        seenOfferKeys.add(key);
      }
    }
  }
}

/**
 * Extract a user-friendly error message from an Error object.
 */
export function intakeFriendlyError(err) {
  if (!err) return "Unknown error";
  const msg = err.message || String(err);
  const match = msg.match(/\d{3}\s+(.*)/);
  return match ? match[1] : msg;
}
