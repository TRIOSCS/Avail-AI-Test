/**
 * rfq-vendor-modal.test.ts — Vitest unit tests for the REAL rfqVendorModal Alpine factory.
 *
 * Imports app/static/htmx_app.js (with htmx/Alpine mocked) and pulls the actual
 * Alpine.data('rfqVendorModal', ...) factory out of the captured registry — so the
 * network/branching logic in confirmSend / loadPreview / _refreshSightings is exercised
 * against the shipped code, not a hand-copied mirror.
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

// Build a real factory instance with the Alpine magics ($store/$dispatch/$refs) injected.
function makeModal(names: string[], ids: number[]) {
  const inst = registry['rfqVendorModal'](names, ids);
  inst.$store = { toast: { message: '', type: 'info', show: false } };
  inst.$dispatch = vi.fn();
  inst.$refs = { previewContent: document.createElement('div') };
  return inst;
}

function fetchResponse(sent: string | null, total: string | null, ok = true, status = 200) {
  const h: Record<string, string | null> = { 'X-RFQ-Sent': sent, 'X-RFQ-Total': total };
  return { ok, status, headers: { get: (k: string) => h[k] ?? null } };
}

beforeEach(async () => {
  registry = {};
  alpineMock.store.mockReset();
  (htmx.ajax as any).mockReset();
  (htmx.ajax as any).mockResolvedValue(undefined);
  document.body.innerHTML = '';
  document.cookie = 'csrftoken=testtoken';
  vi.stubGlobal('fetch', vi.fn());
  vi.resetModules();
  await import('../../app/static/htmx_app.js');
});

describe('rfqVendorModal (real factory)', () => {
  describe('initial state + selection', () => {
    it('seeds selectedVendors from the names and exposes selectedCount/isSelected', () => {
      const m = makeModal(['arrow electronics', 'mouser'], [10, 11]);
      expect(m.selectedCount).toBe(2);
      expect(m.isSelected('arrow electronics')).toBe(true);
      expect(m.step).toBe('compose');
    });

    it('toggleVendor adds and removes', () => {
      const m = makeModal(['mouser'], [1]);
      m.toggleVendor('mouser');
      expect(m.isSelected('mouser')).toBe(false);
      m.toggleVendor('digikey');
      expect(m.selectedCount).toBe(1);
      expect(m.isSelected('digikey')).toBe(true);
    });
  });

  describe('_form serialisation (repeated keys, no Object.fromEntries collapse)', () => {
    it('emits repeated requirement_ids and vendor_names', () => {
      const m = makeModal(['arrow electronics', 'mouser'], [10, 11]);
      m.emailBody = 'hi';
      const form = m._form();
      expect(form.getAll('requirement_ids')).toEqual(['10', '11']);
      expect(form.getAll('vendor_names').sort()).toEqual(['arrow electronics', 'mouser']);
      expect(form.get('email_body')).toBe('hi');
    });
  });

  describe('confirmSend (real network branching)', () => {
    it('full success → success toast, refresh, close', async () => {
      const m = makeModal(['a', 'b'], [1]);
      m.emailBody = 'please quote';
      alpineMock.store.mockReturnValue({ selectedReqId: null });
      (fetch as any).mockResolvedValue(fetchResponse('2', '2'));

      await m.confirmSend();

      expect(m.$store.toast.type).toBe('success');
      expect(m.$store.toast.message).toBe('RFQ sent to 2 vendors');
      expect(m.$dispatch).toHaveBeenCalledWith('close-modal');
      expect(m.sending).toBe(false);
    });

    it('partial failure (1 of 2) → warning toast, still refresh + close', async () => {
      const m = makeModal(['a', 'b'], [1]);
      m.emailBody = 'q';
      alpineMock.store.mockReturnValue({ selectedReqId: null });
      (fetch as any).mockResolvedValue(fetchResponse('1', '2'));

      await m.confirmSend();

      expect(m.$store.toast.type).toBe('warning');
      expect(m.$store.toast.message).toBe('Sent to 1 of 2 vendors — 1 failed');
      expect(m.$dispatch).toHaveBeenCalledWith('close-modal');
    });

    it('nothing delivered (0 sent) → error toast, modal stays OPEN, no refresh', async () => {
      const m = makeModal(['a', 'b'], [1]);
      m.emailBody = 'q';
      (fetch as any).mockResolvedValue(fetchResponse('0', '2'));

      await m.confirmSend();

      expect(m.$store.toast.type).toBe('error');
      expect(m.$dispatch).not.toHaveBeenCalled();   // modal kept open to retry
      expect(htmx.ajax).not.toHaveBeenCalled();      // no refresh
      expect(m.sending).toBe(false);
    });

    it('network failure (fetch rejects) → error toast, sending reset', async () => {
      const m = makeModal(['a'], [1]);
      m.emailBody = 'q';
      (fetch as any).mockRejectedValue(new Error('offline'));

      await m.confirmSend();

      expect(m.$store.toast.type).toBe('error');
      expect(m.$store.toast.message).toBe('Send failed — please try again');
      expect(m.$dispatch).not.toHaveBeenCalled();
      expect(m.sending).toBe(false);
    });

    it('non-2xx → error toast (does not infer success)', async () => {
      const m = makeModal(['a'], [1]);
      m.emailBody = 'q';
      (fetch as any).mockResolvedValue(fetchResponse('1', '1', false, 500));

      await m.confirmSend();

      expect(m.$store.toast.type).toBe('error');
      expect(m.$dispatch).not.toHaveBeenCalled();
    });

    it('guards: no-op when nothing selected or body empty', async () => {
      const empty = makeModal([], [1]);
      empty.emailBody = 'q';
      await empty.confirmSend();
      const noBody = makeModal(['a'], [1]);
      await noBody.confirmSend();
      expect(fetch).not.toHaveBeenCalled();
    });
  });

  describe('loadPreview (real)', () => {
    it('success → step becomes preview', async () => {
      const m = makeModal(['a'], [1]);
      m.emailBody = 'q';
      (htmx.ajax as any).mockResolvedValue(undefined);
      await m.loadPreview();
      expect(m.step).toBe('preview');
      expect(htmx.ajax).toHaveBeenCalled();
    });

    it('failure → error toast, stays on compose', async () => {
      const m = makeModal(['a'], [1]);
      m.emailBody = 'q';
      (htmx.ajax as any).mockRejectedValue(new Error('500'));
      await m.loadPreview();
      expect(m.step).toBe('compose');
      expect(m.$store.toast.type).toBe('error');
      expect(m.previewing).toBe(false);
    });
  });

  describe('_refreshSightings targeting', () => {
    it('refreshes the open detail panel when a requirement is selected', () => {
      const m = makeModal(['a'], [5]);
      alpineMock.store.mockReturnValue({ selectedReqId: 5 });
      m._refreshSightings();
      const detailCall = (htmx.ajax as any).mock.calls.find((c: any[]) => c[2]?.target === '#sightings-detail');
      expect(detailCall).toBeTruthy();
      expect(detailCall[1]).toContain('/v2/partials/sightings/5/detail');
    });

    it('refreshes the requirements table when present', () => {
      const m = makeModal(['a'], [5]);
      alpineMock.store.mockReturnValue({ selectedReqId: null });
      const table = document.createElement('div');
      table.id = 'sightings-table';
      table.setAttribute('hx-get', '/v2/partials/sightings');
      document.body.appendChild(table);
      m._refreshSightings();
      const tableCall = (htmx.ajax as any).mock.calls.find((c: any[]) => c[2]?.target === '#sightings-table');
      expect(tableCall).toBeTruthy();
      expect(tableCall[1]).toBe('/v2/partials/sightings');
    });

    it('does nothing when neither a selected req nor a table is present', () => {
      const m = makeModal(['a'], [5]);
      alpineMock.store.mockReturnValue({ selectedReqId: null });
      m._refreshSightings();
      expect(htmx.ajax).not.toHaveBeenCalled();
    });

    it('warns when a refresh htmx.ajax rejects (network/timeout)', async () => {
      const m = makeModal(['a'], [5]);
      alpineMock.store.mockReturnValue({ selectedReqId: 5 });
      (htmx.ajax as any).mockRejectedValue(new Error('offline'));
      m._refreshSightings();
      await Promise.resolve();
      await Promise.resolve(); // flush the .catch microtask
      expect(m.$store.toast.type).toBe('warning');
      expect(m.$store.toast.message).toContain('refresh the page');
    });
  });

  describe('_sendOutcome mapping', () => {
    it('maps full / partial / zero correctly', () => {
      const m = makeModal(['a'], [1]);
      expect(m._sendOutcome(3, 3)).toMatchObject({ type: 'success', delivered: true });
      expect(m._sendOutcome(1, 1).message).toBe('RFQ sent to 1 vendor');
      expect(m._sendOutcome(1, 3)).toMatchObject({ type: 'warning', delivered: true });
      expect(m._sendOutcome(0, 3)).toMatchObject({ type: 'error', delivered: false });
    });
  });
});
