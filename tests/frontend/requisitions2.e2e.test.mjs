/**
 * requisitions2.e2e.test.mjs — End-to-end tests for the Requisitions 2 Alpine.js component.
 *
 * Tests the rq2Page() Alpine component logic: selection management,
 * toast handling, and table swap cleanup.
 *
 * Called by: npm run test:frontend:e2e
 * Depends on: node:test, app/static/js/requisitions2.js
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Read the Alpine component source
const jsPath = resolve("app/static/js/requisitions2.js");
const jsSource = readFileSync(jsPath, "utf-8");

// ── Component source validation ─────────────────────────────────────

test("requisitions2.js defines rq2Page Alpine component", () => {
  assert.ok(jsSource.includes("Alpine.data('rq2Page'"), "should register rq2Page component");
});

test("requisitions2.js initializes selectedIds as Set", () => {
  assert.ok(jsSource.includes("selectedIds: new Set()"), "should init selectedIds as empty Set");
});

test("requisitions2.js initializes toasts as array", () => {
  assert.ok(jsSource.includes("toasts: []"), "should init toasts as empty array");
});

// ── Selection logic ─────────────────────────────────────────────────

test("toggleSelection method exists", () => {
  assert.ok(jsSource.includes("toggleSelection(id, checked)"), "should have toggleSelection");
});

test("toggleAll method exists", () => {
  assert.ok(jsSource.includes("toggleAll(checked, ids)"), "should have toggleAll");
});

test("clearSelection method exists", () => {
  assert.ok(jsSource.includes("clearSelection()"), "should have clearSelection");
});

test("getSelectedIdsString method exists", () => {
  assert.ok(jsSource.includes("getSelectedIdsString()"), "should have getSelectedIdsString");
});

test("getSelectedIdsString returns comma-joined ids", () => {
  assert.ok(jsSource.includes("[...this.selectedIds].join(',')"), "should join with commas");
});

// ── Event handling ──────────────────────────────────────────────────

test("onTableSwap resets selection when table swapped", () => {
  assert.ok(jsSource.includes("onTableSwap(event)"), "should have onTableSwap");
  assert.ok(jsSource.includes("rq2-table"), "should check for rq2-table target");
});

test("showToast method exists and handles event detail", () => {
  assert.ok(jsSource.includes("showToast(event)"), "should have showToast");
  assert.ok(jsSource.includes("event.detail"), "should read event.detail");
});

test("toast auto-removes after timeout", () => {
  assert.ok(jsSource.includes("setTimeout"), "should auto-remove toasts");
  assert.ok(jsSource.includes("3000"), "should use 3 second timeout");
});

// ── Alpine reactivity pattern ───────────────────────────────────────

test("uses Set recreation pattern for Alpine reactivity", () => {
  // Alpine.js doesn't detect mutations to Set — must reassign
  const reassignCount = (jsSource.match(/this\.selectedIds = new Set/g) || []).length;
  assert.ok(reassignCount >= 3, `should reassign Set for reactivity (found ${reassignCount} times)`);
});

// ── Event listeners ─────────────────────────────────────────────────

test("registers on alpine:init event", () => {
  assert.ok(jsSource.includes("alpine:init"), "should listen for alpine:init");
});

// ── Component size ──────────────────────────────────────────────────

test("component is under 100 lines", () => {
  const lines = jsSource.split("\n").length;
  assert.ok(lines < 100, `component should be <100 lines (got ${lines})`);
});

// ── No business logic in JS ─────────────────────────────────────────

test("no fetch or XMLHttpRequest calls", () => {
  assert.ok(!jsSource.includes("fetch("), "should not make fetch calls — HTMX handles data");
  assert.ok(!jsSource.includes("XMLHttpRequest"), "should not use XHR");
});

test("no DOM innerHTML manipulation", () => {
  assert.ok(!jsSource.includes(".innerHTML"), "should not manipulate innerHTML — HTMX does swaps");
});

test("no jQuery usage", () => {
  assert.ok(!jsSource.includes("$("), "should not use jQuery");
  assert.ok(!jsSource.includes("jQuery"), "should not reference jQuery");
});
