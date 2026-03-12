/**
 * intake_helpers.unit.test.mjs — Unit tests for intake helper functions.
 *
 * Verifies normalization, fallback parsing, and duplicate detection behavior
 * used by the free-form intake drawer.
 *
 * Called by: npm run test:frontend:unit
 * Depends on: node:test, app/static/intake_helpers.mjs
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  applyDuplicateMarkers,
  normalizeIntakeRow,
  offerDuplicateKey,
  parseFallbackRows,
} from "../../app/static/intake_helpers.mjs";

test("normalizeIntakeRow keeps clean typed defaults", () => {
  const row = normalizeIntakeRow(
    {
      row_type: "offer",
      mpn: "  lm317t  ",
      qty: "1000",
      unit_price: "0.45",
      vendor_name: " ACME ",
      confidence: "0.9",
    },
    "auto"
  );
  assert.equal(row.row_type, "offer");
  assert.equal(row.mpn, "lm317t");
  assert.equal(row.qty, "1000");
  assert.equal(row.unit_price, "0.45");
  assert.equal(row.vendor_name, "ACME");
  assert.equal(row.duplicate, false);
});

test("parseFallbackRows builds requirement rows by default", () => {
  const rows = parseFallbackRows("MPN\tQTY\tPRICE\nLM317T\t500\t0.40\nLM7805\t200\t0.25", "rfq");
  assert.equal(rows.length, 2);
  assert.equal(rows[0].row_type, "requirement");
  assert.equal(rows[0].mpn, "LM317T");
  assert.equal(rows[1].unit_price, "0.25");
});

test("offerDuplicateKey uses vendor and mpn", () => {
  const key = offerDuplicateKey({ vendor_name: "Acme", mpn: "LM317T" });
  assert.equal(key, "ACME::LM317T");
});

test("applyDuplicateMarkers flags internal and existing duplicates", () => {
  const rows = [
    normalizeIntakeRow({ row_type: "requirement", mpn: "LM317T" }),
    normalizeIntakeRow({ row_type: "requirement", mpn: "LM317T" }),
    normalizeIntakeRow({ row_type: "offer", mpn: "LM7805", vendor_name: "Acme" }),
    normalizeIntakeRow({ row_type: "offer", mpn: "LM7805", vendor_name: "Acme" }),
  ];
  const existingRequirementMpns = new Set(["NE555"]);
  const existingOfferKeys = new Set(["ACME::LM324"]);
  applyDuplicateMarkers(rows, { existingRequirementMpns, existingOfferKeys });
  assert.equal(rows[0].duplicate, false);
  assert.equal(rows[1].duplicate, true);
  assert.match(rows[1].duplicate_reason, /Duplicate RFQ line/i);
  assert.equal(rows[2].duplicate, false);
  assert.equal(rows[3].duplicate, true);
  assert.match(rows[3].duplicate_reason, /Duplicate offer/i);
});
