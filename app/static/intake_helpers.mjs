/*
 * intake_helpers.mjs — Shared helpers for AI intake parsing/submission.
 *
 * Provides pure, testable utilities for normalizing intake rows, fallback
 * parsing, duplicate detection, and user-friendly save error text.
 *
 * Called by: app/static/app.js
 * Depends on: none
 */

function _toText(v) {
  return v == null ? "" : String(v).trim();
}

function _toNum(v) {
  if (v == null || v === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? String(n) : _toText(v);
}

export function normalizeIntakeRow(raw, mode = "auto") {
  const inferredType = mode === "offer" ? "offer" : "requirement";
  const rowType = _toText(raw?.row_type || raw?.type || inferredType).toLowerCase();
  return {
    row_type: rowType === "offer" ? "offer" : "requirement",
    mpn: _toText(raw?.mpn),
    qty: _toNum(raw?.qty),
    unit_price: _toNum(raw?.unit_price),
    vendor_name: _toText(raw?.vendor_name),
    manufacturer: _toText(raw?.manufacturer),
    lead_time: _toText(raw?.lead_time),
    condition: _toText(raw?.condition),
    packaging: _toText(raw?.packaging),
    notes: _toText(raw?.notes),
    confidence: Number(raw?.confidence ?? 0.5) || 0.5,
    duplicate: false,
    duplicate_reason: "",
    save_status: "",
    save_error: "",
  };
}

export function parseFallbackRows(text, mode = "auto", context = {}) {
  const lines = _toText(text).split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return [];
  const first = lines[0].toLowerCase();
  const hasHeader =
    first.includes("mpn") ||
    first.includes("part") ||
    first.includes("qty") ||
    first.includes("price");
  const start = hasHeader ? 1 : 0;
  const fallbackType = mode === "offer" ? "offer" : "requirement";
  const out = [];
  for (let i = start; i < lines.length; i++) {
    const cols = lines[i].split("\t");
    const mpn = _toText(cols[0]);
    if (!mpn) continue;
    out.push(
      normalizeIntakeRow(
        {
          row_type: fallbackType,
          mpn,
          qty: _toText(cols[1]),
          unit_price: _toText(cols[2]),
          manufacturer: _toText(cols[3]),
          vendor_name: fallbackType === "offer" ? _toText(context?.vendor_name || "Unknown Vendor") : "",
          confidence: 0.4,
        },
        mode
      )
    );
  }
  return out;
}

export function offerDuplicateKey(row) {
  const mpn = _toText(row?.mpn).toUpperCase();
  const vendor = _toText(row?.vendor_name).toUpperCase();
  return `${vendor}::${mpn}`;
}

export function applyDuplicateMarkers(rows, opts = {}) {
  const existingReq = opts.existingRequirementMpns || new Set();
  const existingOffers = opts.existingOfferKeys || new Set();
  const seenReq = new Set();
  const seenOffer = new Set();

  for (const row of rows) {
    row.duplicate = false;
    row.duplicate_reason = "";
    const mpnKey = _toText(row.mpn).toUpperCase();
    if (!mpnKey) continue;

    if (row.row_type === "offer") {
      const key = offerDuplicateKey(row);
      if (seenOffer.has(key)) {
        row.duplicate = true;
        row.duplicate_reason = "Duplicate offer in pasted text";
      } else if (existingOffers.has(key)) {
        row.duplicate = true;
        row.duplicate_reason = "Offer already exists on this requisition";
      }
      seenOffer.add(key);
    } else {
      if (seenReq.has(mpnKey)) {
        row.duplicate = true;
        row.duplicate_reason = "Duplicate RFQ line in pasted text";
      } else if (existingReq.has(mpnKey)) {
        row.duplicate = true;
        row.duplicate_reason = "Part already exists on this requisition";
      }
      seenReq.add(mpnKey);
    }
  }
  return rows;
}

export function intakeFriendlyError(err, fallback = "Save failed") {
  if (!err) return fallback;
  const msg = _toText(err.message || err);
  if (!msg) return fallback;
  if (msg.includes("Duplicate request blocked")) return "Duplicate request blocked";
  if (msg.length > 180) return msg.slice(0, 180);
  return msg;
}
