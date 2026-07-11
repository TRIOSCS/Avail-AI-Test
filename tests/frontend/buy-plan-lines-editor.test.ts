/**
 * buy-plan-lines-editor.test.ts — Vitest unit tests for the REAL buyPlanLinesEditor
 * Alpine factory in app/static/htmx_app.js (whole-plan "Edit plan" mode for the
 * buy-plan line-items table).
 *
 * Imports app/static/htmx_app.js (htmx/Alpine mocked) and pulls the actual
 * Alpine.data('buyPlanLinesEditor', ...) factory from the captured registry, so
 * rowState/invalidRows/buildPayload/known_line_ids/removeRow/cancelEdit/saveAll are
 * exercised against the shipped component, not a hand-copied mirror.
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom, app/static/htmx_app.js
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

let registry: Record<string, any> = {};

// Mock htmx and all Alpine plugins so htmx_app.js imports cleanly in jsdom.
vi.mock('htmx.org', () => ({
  default: {
    on: vi.fn(), off: vi.fn(), ajax: vi.fn(), process: vi.fn(),
    defineExtension: vi.fn(), createExtension: vi.fn(), config: {},
  },
}));

const alpineMock = {
  data: (n: string, f: any) => { registry[n] = f; },
  store: vi.fn(),
  plugin: vi.fn(), start: vi.fn(), directive: vi.fn(), magic: vi.fn(),
};

vi.mock('alpinejs', () => ({ default: alpineMock }));
vi.mock('@alpinejs/focus', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/persist', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/intersect', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/collapse', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/morph', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/mask', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/sort', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/anchor', () => ({ default: vi.fn() }));
vi.mock('@alpinejs/resize', () => ({ default: vi.fn() }));
vi.mock('htmx-ext-alpine-morph', () => ({}));
vi.mock('htmx-ext-response-targets', () => ({}));
vi.mock('htmx-ext-sse', () => ({}));
vi.mock('htmx-ext-json-enc', () => ({}));
vi.mock('htmx-ext-preload', () => ({}));
vi.mock('htmx-ext-loading-states', () => ({}));
vi.mock('htmx-ext-path-params', () => ({}));
vi.mock('htmx-ext-remove-me', () => ({}));

import htmx from 'htmx.org';

// ── Row / seed helpers ──────────────────────────────────────────────────
function row(overrides: Record<string, any> = {}) {
  return {
    _uid: 'line-1',
    lineId: 1,
    requirementId: 100,
    mpn: 'ABC123',
    description: 'Resistor 10k',
    offerId: '',
    vendorName: null,
    qty: '',
    sell: '',
    locked: false,
    removed: false,
    ...overrides,
  };
}

function makeEditor(
  bpId = 1,
  seedRows: any[] = [],
  offersByReq: Record<string, any[]> = {},
  addableParts: any[] = [],
) {
  const inst = registry['buyPlanLinesEditor'](bpId, seedRows, offersByReq, addableParts);
  inst.init();
  return inst;
}

beforeEach(async () => {
  registry = {};
  (htmx.ajax as any).mockReset();
  (htmx.ajax as any).mockResolvedValue(undefined);
  vi.resetModules();
  await import('../../app/static/htmx_app.js');
});

describe('buyPlanLinesEditor (real factory)', () => {
  describe('rowState()', () => {
    it('classifies an existing, fully-filled row as complete', () => {
      const m = makeEditor();
      const r = row({ lineId: 5, offerId: '9', qty: '3' });
      expect(m.rowState(r)).toMatchObject({ isNew: false, hasOffer: true, hasQty: true, complete: true, skip: false });
    });

    it('classifies a locked row via its own offer/qty (isNew stays false since lineId is set)', () => {
      const m = makeEditor();
      const r = row({ lineId: 7, locked: true, offerId: '2', qty: '10' });
      const state = m.rowState(r);
      expect(state.isNew).toBe(false);
      expect(state.complete).toBe(true);
    });

    it('marks an untouched new scratch row (no lineId, no offer, no qty) as skip', () => {
      const m = makeEditor();
      const r = row({ lineId: null, offerId: '', qty: '' });
      expect(m.rowState(r)).toMatchObject({ isNew: true, hasOffer: false, hasQty: false, complete: false, skip: true });
    });

    it('marks a partially-filled new row (offer set, qty blank) as invalid, not skip', () => {
      const m = makeEditor();
      const r = row({ lineId: null, offerId: '4', qty: '' });
      expect(m.rowState(r)).toMatchObject({ isNew: true, hasOffer: true, hasQty: false, complete: false, skip: false });
    });

    it('treats qty 0 or negative as not having a qty', () => {
      const m = makeEditor();
      expect(m.rowState(row({ lineId: null, offerId: '1', qty: '0' })).hasQty).toBe(false);
      expect(m.rowState(row({ lineId: null, offerId: '1', qty: '-1' })).hasQty).toBe(false);
    });
  });

  describe('invalidRows / canSave gating', () => {
    it('flags a non-locked row missing an offer as invalid', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '', qty: '5' })]);
      expect(m.invalidRows).toHaveLength(1);
      expect(m.canSave).toBe(false);
    });

    it('flags a non-locked row with qty < 1 as invalid', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '9', qty: '0' })]);
      expect(m.invalidRows).toHaveLength(1);
      expect(m.canSave).toBe(false);
    });

    it('does not flag a removed row, a locked row, or an untouched scratch row', () => {
      const m = makeEditor(1, [
        row({ lineId: 1, offerId: '', qty: '', removed: true }),
        row({ lineId: 2, locked: true, offerId: '', qty: '' }),
        row({ lineId: null, offerId: '', qty: '' }),
      ]);
      expect(m.invalidRows).toHaveLength(0);
      expect(m.canSave).toBe(true);
    });

    it('canSave is true when every row is complete or skippable', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '9', qty: '5' })]);
      expect(m.canSave).toBe(true);
    });

    it('the saving flag forces canSave false even with zero invalid rows', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '9', qty: '5' })]);
      expect(m.canSave).toBe(true);
      m.saving = true;
      expect(m.invalidRows).toHaveLength(0);
      expect(m.canSave).toBe(false);
    });
  });

  describe('buildPayload()', () => {
    it('locked rows emit ONLY {line_id, unit_sell} — no quantity/offer_id keys', () => {
      const m = makeEditor(1, [row({ lineId: 3, locked: true, offerId: '9', qty: '20', sell: '5.5' })]);
      const payload = m.buildPayload();
      expect(payload.lines).toEqual([{ line_id: 3, unit_sell: 5.5 }]);
    });

    it('non-locked existing rows emit line_id/quantity/unit_sell/offer_id with number coercion', () => {
      // qty/offerId arrive as strings (select/number-input values) — must coerce.
      const m = makeEditor(1, [row({ lineId: 4, offerId: '42', qty: '7', sell: '3.25' })]);
      const payload = m.buildPayload();
      expect(payload.lines).toEqual([{ line_id: 4, quantity: 7, unit_sell: 3.25, offer_id: 42 }]);
      expect(payload.lines[0].quantity).toBe(7);
      expect(typeof payload.lines[0].quantity).toBe('number');
      expect(typeof payload.lines[0].offer_id).toBe('number');
    });

    it('blank sell coerces to null (key-presence "clear" semantics), never an empty string', () => {
      const m = makeEditor(1, [row({ lineId: 4, offerId: '42', qty: '7', sell: '' })]);
      expect(m.buildPayload().lines[0].unit_sell).toBeNull();
    });

    it('removed rows are omitted from the payload entirely', () => {
      const m = makeEditor(1, [
        row({ lineId: 4, offerId: '42', qty: '7', sell: '1', removed: true }),
        row({ lineId: 5, offerId: '9', qty: '2' }),
      ]);
      const payload = m.buildPayload();
      expect(payload.lines).toHaveLength(1);
      expect(payload.lines[0].line_id).toBe(5);
    });

    it('untouched new scratch rows are omitted from the payload', () => {
      const m = makeEditor(1, [
        row({ lineId: null, offerId: '', qty: '' }),
        row({ lineId: 5, offerId: '9', qty: '2' }),
      ]);
      const payload = m.buildPayload();
      expect(payload.lines).toHaveLength(1);
      expect(payload.lines[0].line_id).toBe(5);
    });

    it('new (unsaved) rows emit requirement_id/offer_id/quantity/unit_sell — no line_id key', () => {
      const m = makeEditor(1, [row({ lineId: null, requirementId: 200, offerId: '11', qty: '3', sell: '' })]);
      const payload = m.buildPayload();
      expect(payload.lines).toEqual([{ requirement_id: 200, offer_id: 11, quantity: 3, unit_sell: null }]);
      expect(payload.lines[0]).not.toHaveProperty('line_id');
    });

    it('known_line_ids equals the mount-snapshot ids — including a later soft-removed line and a locked line — and ignores a new unsaved row', () => {
      const m = makeEditor(1, [
        row({ lineId: 1, offerId: '9', qty: '5' }),
        row({ lineId: 2, locked: true, offerId: '9', qty: '1' }),
        row({ lineId: null, offerId: '', qty: '' }),
      ]);
      // Soft-remove line 1 AFTER mount — origRows (the snapshot) must be unaffected.
      m.removeRow(m.rows[0]);
      // Add a brand-new row after mount — must never appear in known_line_ids.
      m.addVendorRow(100, 'ABC', 'desc');

      const payload = m.buildPayload();
      expect(payload.known_line_ids.slice().sort()).toEqual([1, 2]);
    });
  });

  describe('removeRow / undoRemove', () => {
    it('soft-removes a persisted row (kept in rows, removed=true)', () => {
      const m = makeEditor(1, [row({ lineId: 1 })]);
      m.removeRow(m.rows[0]);
      expect(m.rows).toHaveLength(1);
      expect(m.rows[0].removed).toBe(true);
    });

    it('hard-deletes an unsaved scratch row (spliced out of rows)', () => {
      const m = makeEditor(1, [row({ lineId: null, _uid: 'new-1' })]);
      m.removeRow(m.rows[0]);
      expect(m.rows).toHaveLength(0);
    });

    it('undoRemove clears the removed flag on a soft-removed row', () => {
      const m = makeEditor(1, [row({ lineId: 1 })]);
      m.removeRow(m.rows[0]);
      expect(m.rows[0].removed).toBe(true);
      m.undoRemove(m.rows[0]);
      expect(m.rows[0].removed).toBe(false);
    });
  });

  describe('addVendorRow / addLineFromPicker', () => {
    it('addVendorRow pushes a correctly-shaped blank scratch row for the given part', () => {
      const m = makeEditor(1, []);
      m.addVendorRow(300, 'XYZ999', 'Capacitor');
      expect(m.rows).toHaveLength(1);
      expect(m.rows[0]).toMatchObject({
        lineId: null, requirementId: 300, mpn: 'XYZ999', description: 'Capacitor',
        offerId: '', qty: '', sell: '', locked: false, removed: false,
      });
      expect(m.rows[0]._uid).toBeTruthy();
    });

    it('addLineFromPicker no-ops when offer, requirement, or qty is missing', () => {
      const m = makeEditor(1, [], {}, [{ id: 300, mpn: 'XYZ999', description: 'Capacitor' }]);
      m.newPart = { reqId: '', offerId: '11', qty: '5', sell: '' };
      m.addLineFromPicker();
      expect(m.rows).toHaveLength(0);
    });

    it('addLineFromPicker pushes a fully-shaped new row from the addableParts lookup and resets the picker', () => {
      const m = makeEditor(1, [], {}, [{ id: 300, mpn: 'XYZ999', description: 'Capacitor' }]);
      m.showAddLine = true;
      m.newPart = { reqId: '300', offerId: '11', qty: '5', sell: '2.5' };
      m.addLineFromPicker();
      expect(m.rows).toHaveLength(1);
      expect(m.rows[0]).toMatchObject({
        lineId: null, requirementId: 300, mpn: 'XYZ999', description: 'Capacitor',
        offerId: '11', qty: '5', sell: '2.5', locked: false, removed: false,
      });
      expect(m.newPart).toEqual({ reqId: '', offerId: '', qty: '', sell: '' });
      expect(m.showAddLine).toBe(false);
    });
  });

  describe('cancelEdit — restores the untouched mount snapshot (deep copy)', () => {
    it('reverts an in-place row mutation back to the seeded value', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '9', qty: '5', sell: '1.5' })]);
      m.enterEdit();
      expect(m.editMode).toBe(true);

      m.rows[0].qty = 999;
      m.rows[0].sell = 777;
      m.rows[0].offerId = 'mutated';

      m.cancelEdit();

      expect(m.editMode).toBe(false);
      expect(m.rows[0]).toMatchObject({ qty: '5', sell: '1.5', offerId: '9' });
    });

    it('mutating a live row never leaks back into origRows (proves the snapshot is a deep copy)', () => {
      const m = makeEditor(1, [row({ lineId: 1, qty: '5' })]);
      m.rows[0].qty = 999;
      expect(m.origRows[0].qty).toBe('5');
    });

    it('discards rows added after mount and restores soft-removed rows on cancel', () => {
      const m = makeEditor(1, [row({ lineId: 1 })]);
      m.addVendorRow(100, 'ABC', 'desc');
      m.removeRow(m.rows[0]);
      expect(m.rows).toHaveLength(2);

      m.cancelEdit();

      expect(m.rows).toHaveLength(1);
      expect(m.rows[0].lineId).toBe(1);
      expect(m.rows[0].removed).toBe(false);
    });
  });

  describe('saveAll — double-submit guard + saving reset', () => {
    it('a second synchronous call while saving does not trigger a second htmx.ajax', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '9', qty: '5' })]);
      m.saveAll();
      m.saveAll();
      expect((htmx.ajax as any).mock.calls).toHaveLength(1);
    });

    it('posts to the bulk endpoint with the JSON payload string and resets saving on success', async () => {
      const m = makeEditor(7, [row({ lineId: 1, offerId: '9', qty: '5', sell: '2' })]);
      m.saveAll();
      expect(m.saving).toBe(true);
      const [method, url, opts] = (htmx.ajax as any).mock.calls[0];
      expect(method).toBe('POST');
      expect(url).toBe('/v2/partials/buy-plans/7/lines/bulk');
      expect(opts.target).toBe('#main-content');
      const sent = JSON.parse(opts.values.payload);
      expect(sent.lines).toEqual([{ line_id: 1, quantity: 5, unit_sell: 2, offer_id: 9 }]);
      expect(sent.known_line_ids).toEqual([1]);

      await vi.waitFor(() => expect(m.saving).toBe(false));
    });

    it('resets saving when the htmx.ajax promise rejects (e.g. a 400)', async () => {
      (htmx.ajax as any).mockRejectedValueOnce(new Error('bad request'));
      const m = makeEditor(1, [row({ lineId: 1, offerId: '9', qty: '5' })]);
      m.saveAll();
      expect(m.saving).toBe(true);
      await vi.waitFor(() => expect(m.saving).toBe(false));
    });

    it('does nothing when canSave is false (invalid row present)', () => {
      const m = makeEditor(1, [row({ lineId: 1, offerId: '', qty: '5' })]);
      m.saveAll();
      expect(htmx.ajax).not.toHaveBeenCalled();
      expect(m.saving).toBe(false);
    });
  });
});
