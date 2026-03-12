/**
 * intake_intakeflow.e2e.test.mjs — Workflow-level tests for intake editing/submission state.
 *
 * Simulates an end-to-end intake row lifecycle: parse fallback rows, detect
 * duplicates against existing data, edit to resolve duplicates, and map row
 * save outcomes.
 *
 * Called by: npm run test:frontend:e2e
 * Depends on: node:test, app/static/intake_helpers.mjs
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  applyDuplicateMarkers,
  intakeFriendlyError,
  normalizeIntakeRow,
  offerDuplicateKey,
  parseFallbackRows,
} from "../../app/static/intake_helpers.mjs";

test("intake workflow resolves duplicates after user edits", () => {
  const context = { vendor_name: "Acme Components" };
  const parsed = parseFallbackRows("LM317T\t1000\t0.45\nLM317T\t900\t0.44", "offer", context);
  assert.equal(parsed.length, 2);
  assert.equal(parsed[0].vendor_name, "Acme Components");

  const existingRequirementMpns = new Set();
  const existingOfferKeys = new Set([offerDuplicateKey({ vendor_name: "Acme Components", mpn: "LM317T" })]);
  applyDuplicateMarkers(parsed, { existingRequirementMpns, existingOfferKeys });

  assert.equal(parsed[0].duplicate, true);
  assert.equal(parsed[1].duplicate, true);

  // User edits row 2 (MPN) and row 1 (vendor) in the drawer.
  parsed[1] = normalizeIntakeRow({ ...parsed[1], mpn: "LM7805" }, "offer");
  parsed[0] = normalizeIntakeRow({ ...parsed[0], vendor_name: "Beta Supply" }, "offer");
  applyDuplicateMarkers(parsed, { existingRequirementMpns, existingOfferKeys });

  assert.equal(parsed[0].duplicate, false);
  assert.equal(parsed[1].duplicate, false);
});

test("intake workflow preserves per-row save errors", () => {
  const rows = [
    normalizeIntakeRow({ row_type: "requirement", mpn: "LM317T", qty: "1000" }, "rfq"),
    normalizeIntakeRow({ row_type: "offer", mpn: "LM7805", vendor_name: "Acme" }, "offer"),
  ];
  rows[0].save_status = "saved";
  rows[1].save_status = "error";
  rows[1].save_error = intakeFriendlyError(new Error("422 validation error: vendor_name required"));

  assert.equal(rows[0].save_status, "saved");
  assert.equal(rows[1].save_status, "error");
  assert.match(rows[1].save_error, /vendor_name required/i);
});
