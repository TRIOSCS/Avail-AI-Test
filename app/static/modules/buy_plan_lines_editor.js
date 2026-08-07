/**
 * buy_plan_lines_editor.js — the buyPlanLinesEditor Alpine.data component:
 * whole-table "Edit plan" mode for buy-plan line items (client-side rows array,
 * add/remove/undo, validity gating, and the single bulk-save POST).
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: htmx.org, alpinejs.
 */

import htmx from 'htmx.org';
import Alpine from 'alpinejs';

/**
 * buyPlanLinesEditor — whole-table "Edit plan" mode for the buy-plan line-items
 * table. Replaces the old per-row Edit/Remove toggles + bottom Add-line panel
 * with one editMode flag, a client-side `rows` array (seeded from the server),
 * and a single "Save all" POST.
 *
 * Called by: partials/buy_plans/_detail_lines.html (x-data on the Line Items card).
 * Depends on: Alpine.js, htmx (htmx.ajax posts the bulk payload).
 *
 * `rows` entries: { _uid, lineId, requirementId, mpn, description, offerId,
 * vendorName, unitCost, qty, sell, locked, removed }. `lineId === null` marks a
 * not-yet-saved new line (split-vendor add or the bottom part picker). `vendorName`/
 * `unitCost` are the row's CURRENT offer display data (from the server line at mount)
 * — used as a fallback when that offer isn't in the ACTIVE `offersByReq` map (a
 * locked/PO-cut row, or a non-locked row whose offer went stale/sold-out): the vendor
 * select renders it as an extra "(no longer active)" option instead of blanking out,
 * and `unitCostFor` falls back to it instead of showing '-' for a real stored cost.
 * `locked` mirrors the server's PO-cut gate (po_confirmed_at set or status !=
 * 'awaiting_po') — locked rows only allow editing `sell`.
 *
 * Save posts POST /v2/partials/buy-plans/{bpId}/lines/bulk with
 * {payload: JSON.stringify({lines: [...], known_line_ids: [...]})}. Removed rows are
 * simply omitted from `lines` (removal-by-omission, scoped to `known_line_ids` — see
 * `knownLineIds` below); locked rows send only {line_id, unit_sell} so the server
 * never sees a forbidden field on a cut-PO line. `unit_sell` is always sent (null
 * when the Sell input is blank) since the server treats a present-but-null value as
 * "clear it" and an absent key as "leave unchanged".
 */
Alpine.data('buyPlanLinesEditor', (bpId, seedRows, offersByReq, addableParts) => ({
  bpId,
  offersByReq: offersByReq || {},
  addableParts: addableParts || [],
  rows: seedRows || [],
  origRows: [],
  editMode: false,
  saving: false,
  showAddLine: false,
  newPart: { reqId: '', offerId: '', qty: '', sell: '' },
  _uidCounter: 0,

  init() {
    this._uidCounter = this.rows.length;
    this.origRows = JSON.parse(JSON.stringify(this.rows));
  },

  enterEdit() { this.editMode = true; },

  cancelEdit() {
    this.rows = JSON.parse(JSON.stringify(this.origRows));
    this.newPart = { reqId: '', offerId: '', qty: '', sell: '' };
    this.showAddLine = false;
    this.editMode = false;
  },

  // Rows grouped by requirement, in first-appearance order — drives the
  // per-part "+ Add vendor" affordance after each part's row block.
  get groupedRows() {
    const order = [];
    const map = {};
    for (const r of this.rows) {
      if (!map[r.requirementId]) {
        map[r.requirementId] = { requirementId: r.requirementId, mpn: r.mpn, description: r.description, rows: [] };
        order.push(map[r.requirementId]);
      }
      map[r.requirementId].rows.push(r);
    }
    return order;
  },

  offersFor(reqId) { return this.offersByReq[reqId] || []; },

  // Falls back to the row's own seeded unitCost when the row's offer isn't in the
  // ACTIVE offers map (locked/PO-cut rows, or a stale/sold-out offer still selected on
  // a non-locked row) — otherwise a real stored cost silently renders as '-'/null.
  unitCostFor(row) {
    const offer = this.offersFor(row.requirementId).find((o) => String(o.id) === String(row.offerId));
    if (offer) return offer.unitPrice;
    return row.unitCost !== undefined ? row.unitCost : null;
  },

  fmtMoney(v) {
    if (v === null || v === undefined || v === '') return '-';
    return '$' + Number(v).toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  },

  addVendorRow(reqId, mpn, description) {
    this._uidCounter += 1;
    this.rows.push({
      _uid: 'new-' + this._uidCounter,
      lineId: null,
      requirementId: reqId,
      mpn,
      description,
      offerId: '',
      vendorName: null,
      unitCost: null,
      qty: '',
      sell: '',
      locked: false,
      removed: false,
    });
  },

  // Bottom "+ Add line" part picker (#5 — single offer-render path): a part <select>
  // (x-model="newPart.reqId") followed by an offer <select> over offersFor(newPart.reqId)
  // — the SAME client-side addableParts/offersFor the inline "+ Add vendor" affordance
  // reads, so there is only one place the addable universe is computed.
  addLineFromPicker() {
    if (!this.newPart.offerId || !this.newPart.reqId || !this.newPart.qty) return;
    const part = this.addableParts.find((p) => String(p.id) === String(this.newPart.reqId));
    this._uidCounter += 1;
    this.rows.push({
      _uid: 'new-' + this._uidCounter,
      lineId: null,
      requirementId: Number(this.newPart.reqId),
      mpn: part ? part.mpn : null,
      description: part ? part.description : '',
      offerId: this.newPart.offerId,
      vendorName: null,
      unitCost: null,
      qty: this.newPart.qty,
      sell: this.newPart.sell,
      locked: false,
      removed: false,
    });
    this.newPart = { reqId: '', offerId: '', qty: '', sell: '' };
    this.showAddLine = false;
  },

  removeRow(row) {
    // An unsaved new row has no server-side line to preserve — drop it outright.
    // A persisted row is soft-removed (kept, struck-through, Undo-able) so Save
    // can omit it by id instead of racing a separate delete request.
    if (!row.lineId) {
      this.rows = this.rows.filter((r) => r !== row);
    } else {
      row.removed = true;
    }
  },

  undoRemove(row) { row.removed = false; },

  // Single definition of "complete" vs "skippable scratch" for a row — consumed by
  // both `invalidRows` (Save-enablement) and `buildPayload` (what actually gets
  // posted), so the two can never disagree about which rows count.
  //   - complete: has both an offer and a whole-number qty >= 1 (locked rows don't
  //     need this — callers gate on `r.locked` themselves before checking it).
  //   - skip: an untouched new scratch row (freshly pushed by "+ Add vendor"/the
  //     bottom picker, not yet filled in) — silently ignored rather than invalid.
  rowState(r) {
    const isNew = !r.lineId;
    const hasOffer = r.offerId !== '' && r.offerId !== null && r.offerId !== undefined;
    // A fractional qty (e.g. 2.5) must invalidate the row client-side rather than
    // 400ing the whole bulk save server-side — the server keeps its own guard as the
    // backstop (the error-toast path still covers any client/server disagreement).
    const hasQty = r.qty !== '' && r.qty !== null && Number(r.qty) >= 1 && Number.isInteger(Number(r.qty));
    return { isNew, hasOffer, hasQty, complete: hasOffer && hasQty, skip: isNew && !hasOffer && !hasQty };
  },

  // Non-removed, non-locked rows must carry both an offer and qty >= 1 before
  // Save is allowed — EXCEPT an untouched blank scratch row, which is silently
  // skipped instead (see `rowState`).
  get invalidRows() {
    return this.rows.filter((r) => {
      if (r.removed || r.locked) return false;
      const { skip, complete } = this.rowState(r);
      return !skip && !complete;
    });
  },

  get canSave() { return !this.saving && this.invalidRows.length === 0; },

  // known_line_ids echoes every existing line id the form had AT MOUNT (from the
  // untouched origRows snapshot, not the live/edited `rows` array) — including
  // locked lines and lines the user soft-removed in this session. The server only
  // removes-by-omission a line whose id appears in known_line_ids, so a line another
  // user added concurrently (never in this snapshot, so never "known") can't be
  // silently deleted by this save.
  get knownLineIds() {
    return this.origRows.filter((r) => r.lineId !== null && r.lineId !== undefined).map((r) => r.lineId);
  },

  buildPayload() {
    const lines = [];
    for (const r of this.rows) {
      if (r.removed) continue;
      const { isNew, skip } = this.rowState(r);
      if (skip) continue;
      // unit_sell uses key-presence semantics server-side (key present + null =
      // clear the sell; key absent = leave unchanged) — always send the key, with
      // null when the input is blank, so blanking Sell explicitly clears it.
      const sellVal = (r.sell === '' || r.sell === null || r.sell === undefined) ? null : Number(r.sell);
      if (r.locked) {
        lines.push({ line_id: r.lineId, unit_sell: sellVal });
      } else if (isNew) {
        lines.push({ requirement_id: r.requirementId, offer_id: Number(r.offerId), quantity: Number(r.qty), unit_sell: sellVal });
      } else {
        lines.push({ line_id: r.lineId, quantity: Number(r.qty), unit_sell: sellVal, offer_id: Number(r.offerId) });
      }
    }
    return { lines, known_line_ids: this.knownLineIds };
  },

  // Double-submit guard: `canSave` folds in `!this.saving`, so setting `saving = true`
  // BEFORE the htmx.ajax call synchronously flips `canSave` to false — a rapid second
  // click re-invokes saveAll() (click events are processed one at a time, not
  // concurrently) and is turned away by the guard below regardless of whether the
  // DOM's `:disabled="!canSave"` binding has repainted yet. `data-loading-disable`
  // (htmx-ext-loading-states) only disables elements tied to the request's triggering
  // element; a programmatic ajax call like this one has no such element, so it
  // is NOT relied on here — this explicit `saving` flag is the real guard. Reset in
  // .finally() so a failed (e.g. 400) response doesn't strand the button disabled; a
  // successful save re-renders #main-content, which discards this component entirely.
  saveAll() {
    if (!this.canSave) return;
    this.saving = true;
    htmx.ajax('POST', `/v2/partials/buy-plans/${this.bpId}/lines/bulk`, {
      target: '#main-content',
      swap: 'innerHTML',
      indicator: '#main-content',
      values: { payload: JSON.stringify(this.buildPayload()) },
    }).finally(() => { this.saving = false; });
  },
}));
