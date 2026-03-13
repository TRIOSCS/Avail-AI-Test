/**
 * intake_helpers.unit.test.mjs — Unit tests for intake helper functions.
 *
 * Verifies normalization, fallback parsing, and duplicate detection behavior
 * used by the free-form intake drawer.
 *
 * Called by: npm run test:frontend:unit
 * Depends on: vitest, app/static/intake_helpers.mjs
 */

import { describe, expect, it } from "vitest";

import {
  applyDuplicateMarkers,
  normalizeIntakeRow,
  offerDuplicateKey,
  parseFallbackRows,
} from "../../app/static/intake_helpers.mjs";

describe("intake_helpers", () => {
  it("normalizeIntakeRow keeps clean typed defaults", () => {
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
    expect(row.row_type).toBe("offer");
    expect(row.mpn).toBe("lm317t");
    expect(row.qty).toBe("1000");
    expect(row.unit_price).toBe("0.45");
    expect(row.vendor_name).toBe("ACME");
    expect(row.duplicate).toBe(false);
  });

  it("parseFallbackRows builds requirement rows by default", () => {
    const rows = parseFallbackRows("MPN\tQTY\tPRICE\nLM317T\t500\t0.40\nLM7805\t200\t0.25", "rfq");
    expect(rows).toHaveLength(2);
    expect(rows[0].row_type).toBe("requirement");
    expect(rows[0].mpn).toBe("LM317T");
    expect(rows[1].unit_price).toBe("0.25");
  });

  it("offerDuplicateKey uses vendor and mpn", () => {
    const key = offerDuplicateKey({ vendor_name: "Acme", mpn: "LM317T" });
    expect(key).toBe("ACME::LM317T");
  });

  it("applyDuplicateMarkers flags internal and existing duplicates", () => {
    const rows = [
      normalizeIntakeRow({ row_type: "requirement", mpn: "LM317T" }),
      normalizeIntakeRow({ row_type: "requirement", mpn: "LM317T" }),
      normalizeIntakeRow({ row_type: "offer", mpn: "LM7805", vendor_name: "Acme" }),
      normalizeIntakeRow({ row_type: "offer", mpn: "LM7805", vendor_name: "Acme" }),
    ];
    const existingRequirementMpns = new Set(["NE555"]);
    const existingOfferKeys = new Set(["ACME::LM324"]);
    applyDuplicateMarkers(rows, { existingRequirementMpns, existingOfferKeys });
    expect(rows[0].duplicate).toBe(false);
    expect(rows[1].duplicate).toBe(true);
    expect(rows[1].duplicate_reason).toMatch(/Duplicate RFQ line/i);
    expect(rows[2].duplicate).toBe(false);
    expect(rows[3].duplicate).toBe(true);
    expect(rows[3].duplicate_reason).toMatch(/Duplicate offer/i);
  });
});
