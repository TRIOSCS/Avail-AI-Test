/**
 * offer-picker.test.ts — Vitest unit tests for the REAL offerPicker Alpine factory
 * in app/static/htmx_app.js (the Deal Sheet's ONE lines component — offers grouped by
 * part, check = buy, qty right, sell per group, one atomic bulk save).
 *
 * Successor to buy-plan-lines-editor.test.ts (that component retired with the legacy
 * detail page, Deal Sheet T3b). Imports app/static/htmx_app.js (htmx/Alpine mocked)
 * and pulls the actual Alpine.data('offerPicker', ...) factory from the captured
 * registry, so init/dirty/toggle/filled/margins/invalid/saveAll are exercised against
 * the shipped component, not a hand-copied mirror.
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

// ── Seed helpers (mirror _sheet_ctx's picker_groups shape) ─────────────
function offer(overrides: Record<string, any> = {}) {
  return {
    offer_id: 11,
    vendor: 'Arrow',
    cost: 1.71,
    condition: 'new',
    avail: 1200,
    active: true,
    line_id: null,
    qty: null,
    locked: false,
    best: true,
    ...overrides,
  };
}

function group(overrides: Record<string, any> = {}) {
  return {
    requirement_id: 100,
    mpn: 'LM317T',
    need: 1000,
    sell: 2.1,
    offers: [offer()],
    ...overrides,
  };
}

function makePicker(
  groups: any[] = [group()],
  knownLineIds: number[] = [],
  opts: Record<string, any> = { lens: 'sales-orders', staleToken: 'tok-1' },
  bpId = 1,
) {
  const inst = registry['offerPicker'](bpId, groups, knownLineIds, opts);
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

describe('offerPicker (real factory)', () => {
  describe('init / dirty', () => {
    it('is clean after init and seeds checked from line_id presence', () => {
      const m = makePicker([group({ offers: [offer({ line_id: 5, qty: 800 })] })]);
      expect(m.dirty).toBe(false);
      expect(m.groups[0].offers[0].checked).toBe(true);
      expect(m.groups[0].offers[0].qty).toBe('800');
    });

    it('goes dirty on a qty change and clean again on discard', () => {
      const m = makePicker([group({ offers: [offer({ line_id: 5, qty: 800 })] })]);
      m.groups[0].offers[0].qty = '900';
      expect(m.dirty).toBe(true);
      m.discard();
      expect(m.dirty).toBe(false);
      expect(m.groups[0].offers[0].qty).toBe('800');
    });
  });

  describe('toggle()', () => {
    it('checking prefills qty with the remaining need', () => {
      const m = makePicker([group({
        need: 1000,
        offers: [offer({ line_id: 5, qty: 800 }), offer({ offer_id: 12, vendor: 'WinSource', best: false })],
      })]);
      m.toggle(m.groups[0].offers[1], m.groups[0]);
      expect(m.groups[0].offers[1].checked).toBe(true);
      expect(m.groups[0].offers[1].qty).toBe('200'); // 1000 need − 800 already filled
    });

    it('never toggles a locked (PO-cut) row', () => {
      const m = makePicker([group({ offers: [offer({ line_id: 5, qty: 500, locked: true })] })]);
      m.toggle(m.groups[0].offers[0], m.groups[0]);
      expect(m.groups[0].offers[0].checked).toBe(true); // unchanged
      expect(m.dirty).toBe(false);
    });
  });

  describe('validation (invalid / canSave)', () => {
    it('flags a checked row without a positive whole qty', () => {
      const m = makePicker();
      m.toggle(m.groups[0].offers[0], m.groups[0]);
      m.groups[0].offers[0].qty = '0';
      expect(m.invalid).toContain('LM317T');
      expect(m.canSave).toBe(false);
    });

    it("flags a non-numeric sell — '1,200' must never save as 1", () => {
      const m = makePicker([group({ offers: [offer({ line_id: 5, qty: 800 })] })]);
      m.groups[0].sell = '1,200';
      expect(m.invalid).toContain('LM317T');
      m.groups[0].sell = '$12';
      expect(m.invalid).toContain('LM317T');
      m.groups[0].sell = '2.25';
      expect(m.invalid).toEqual([]);
      expect(m.canSave).toBe(true);
    });

    it('an EMPTY sell is allowed (explicit clear, not an error)', () => {
      const m = makePicker([group({ offers: [offer({ line_id: 5, qty: 800 })] })]);
      m.groups[0].sell = '';
      expect(m.invalid).toEqual([]);
    });
  });

  describe('filled / money math', () => {
    it('filled() sums only checked qtys; totals/margin follow the checked set', () => {
      const m = makePicker([group({
        sell: 2.0,
        offers: [
          offer({ line_id: 5, qty: 800, cost: 1.5 }),
          offer({ offer_id: 12, cost: 1.0, best: false }),
        ],
      })]);
      expect(m.filled(m.groups[0])).toBe(800);
      expect(m.totals).toMatchObject({ cost: 1200, rev: 1600, margin: 25 });
      expect(m.groupMargin(m.groups[0])).toBe(25);
    });
  });

  describe('saveAll()', () => {
    it('posts the atomic bulk payload: checked rows in, unchecked omitted, locked always ride with line_id + sell only', () => {
      const m = makePicker(
        [group({
          sell: 2.5,
          offers: [
            offer({ line_id: 5, qty: 700 }),                                   // checked, editable
            offer({ offer_id: 12, vendor: 'WinSource', best: false }),          // unchecked → omitted (removal-by-omission scope)
            offer({ offer_id: 13, line_id: 9, qty: 300, locked: true }),        // PO-cut → line_id + unit_sell only
          ],
        })],
        [5, 9],
      );
      m.groups[0].offers[0].qty = '750'; // make it dirty
      m.saveAll();

      expect(htmx.ajax).toHaveBeenCalledTimes(1);
      const [method, url, cfg] = (htmx.ajax as any).mock.calls[0];
      expect(method).toBe('POST');
      expect(url).toBe('/v2/partials/buy-plans/1/lines/bulk');
      expect(cfg.values.origin).toBe('approvals_workspace');
      expect(cfg.values.lens).toBe('sales-orders');
      expect(cfg.values.expected_updated_at).toBe('tok-1');

      const payload = JSON.parse(cfg.values.payload);
      expect(payload.known_line_ids).toEqual([5, 9]);
      expect(payload.lines).toEqual([
        { offer_id: 11, quantity: 750, unit_sell: 2.5, requirement_id: 100, line_id: 5 },
        { line_id: 9, unit_sell: 2.5 },
      ]);
    });

    it('never posts while invalid or clean', () => {
      const m = makePicker([group({ offers: [offer({ line_id: 5, qty: 800 })] })]);
      m.saveAll(); // clean → no-op
      m.groups[0].offers[0].qty = 'abc'; // dirty but invalid
      m.saveAll();
      expect(htmx.ajax).not.toHaveBeenCalled();
    });

    it('a checked NEW row (no line_id) posts offer/quantity/requirement without line_id', () => {
      const m = makePicker([group({ sell: 3, offers: [offer()] })]);
      m.toggle(m.groups[0].offers[0], m.groups[0]); // prefills qty=1000 (full need)
      m.saveAll();
      const payload = JSON.parse((htmx.ajax as any).mock.calls[0][2].values.payload);
      expect(payload.lines).toEqual([
        { offer_id: 11, quantity: 1000, unit_sell: 3, requirement_id: 100 },
      ]);
    });
  });
});
