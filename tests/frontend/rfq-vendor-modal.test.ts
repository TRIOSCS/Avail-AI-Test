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

function fetchResponse(
  sent: string | null,
  total: string | null,
  ok = true,
  status = 200,
  extra: Record<string, string> = {},
) {
  const h: Record<string, string | null> = { 'X-RFQ-Sent': sent, 'X-RFQ-Total': total, ...extra };
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

    it('selectedCount counts only the seeded (contactable) names — C1', () => {
      // C1: the template now passes ONLY contactable normalized names into the factory.
      // The seed is the source of truth for selectedCount / the Send/Preview buttons, so
      // a non-contactable vendor (never seeded) is never counted or posted.
      const m = makeModal(['arrow electronics', 'mouser'], [1]);
      expect(m.selectedCount).toBe(2);
      expect(m.isSelected('arrow electronics')).toBe(true);
      expect(m.isSelected('cardless distributor')).toBe(false);
      // The non-contactable name is absent from _form's vendor_names payload entirely.
      m.emailBody = 'hi';
      expect(m._form().getAll('vendor_names').sort()).toEqual(['arrow electronics', 'mouser']);
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

    it('unavailable vendors are not counted as failed in the toast', async () => {
      // F3: server drops 1 of 2 vendors at send time (X-RFQ-Unavailable=1). The blocked
      // vendor was correctly handled, NOT a delivery failure — the toast must say
      // "marked unavailable", never "1 failed".
      const m = makeModal(['a', 'b'], [1]);
      m.emailBody = 'q';
      alpineMock.store.mockReturnValue({ selectedReqId: null });
      (fetch as any).mockResolvedValue(fetchResponse('1', '2', true, 200, { 'X-RFQ-Unavailable': '1' }));

      await m.confirmSend();

      expect(m.$store.toast.type).toBe('warning');
      expect(m.$store.toast.message).toBe('Sent to 1 of 2 vendors — 1 marked unavailable');
      expect(m.$store.toast.message).not.toContain('failed');
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

  describe('_addComposerVendor / pickVendor (any-vendor picker)', () => {
    function rowHtml(normalized: string) {
      // Mirrors composer_vendor_row.html's selectable branch: x-init carries the
      // server-normalized name as a tojson-quoted string in a single-quoted attr.
      // Jinja's |tojson escapes ' < > & as \uXXXX so the attr can't break — mirror it.
      const quoted = JSON.stringify(normalized)
        .replace(/'/g, '\\u0027').replace(/</g, '\\u003c')
        .replace(/>/g, '\\u003e').replace(/&/g, '\\u0026');
      return `<label x-init='selectVendor(${quoted})'><input type="checkbox"><span>row</span></label>`;
    }
    function htmlResponse(html: string, ok = true, status = 200) {
      return { ok, status, text: () => Promise.resolve(html) };
    }
    function addContainer() {
      const div = document.createElement('div');
      div.id = 'rfq-added-vendors';
      document.body.appendChild(div);
      return div;
    }

    it('pickVendor posts to composer-vendor and appends the returned row', async () => {
      const container = addContainer();
      const m = makeModal(['mouser electronics'], [10, 11]);
      (fetch as any).mockResolvedValue(htmlResponse(rowHtml('new vendor co')));

      await m.pickVendor('New Vendor Co');

      const [url, opts] = (fetch as any).mock.calls[0];
      expect(url).toBe('/v2/partials/sightings/composer-vendor');
      expect(opts.method).toBe('POST');
      expect(opts.headers['x-csrftoken']).toBe('testtoken');
      expect(opts.body.get('vendor_name')).toBe('New Vendor Co');
      expect(opts.body.getAll('requirement_ids')).toEqual(['10', '11']);
      expect(container.children.length).toBe(1);
      expect((htmx as any).process).toHaveBeenCalledWith(container);
      expect(m.vendorQuery).toBe('');
      expect(m.searchOpen).toBe(false);
    });

    it('skips the request entirely when the vendor is already selected (case-insensitive)', async () => {
      const container = addContainer();
      const m = makeModal(['mouser electronics'], [1]);

      await m.pickVendor('Mouser Electronics');

      expect(fetch).not.toHaveBeenCalled();
      expect(container.children.length).toBe(0);
      expect(m.$store.toast.type).toBe('info');
      expect(m.$store.toast.message).toBe('Vendor already added');
    });

    it('skips the APPEND when the returned row resolves to an already-selected vendor', async () => {
      const container = addContainer();
      const m = makeModal(['mouser electronics'], [1]);
      // Display name differs from the normalized key, so the fast path misses and
      // the request goes out — the server-normalized name in the row must dedupe.
      (fetch as any).mockResolvedValue(htmlResponse(rowHtml('mouser electronics')));

      const ok = await m._addComposerVendor({ vendor_name: 'Mouser Electronics, Inc.' });

      expect(ok).toBe(true);
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(container.children.length).toBe(0); // no duplicate visual row
      expect(m.$store.toast.type).toBe('info');
      expect(m.$store.toast.message).toBe('Vendor already added');
    });

    it('still appends excluded rows (no x-init selection key)', async () => {
      const container = addContainer();
      const m = makeModal(['mouser electronics'], [1]);
      (fetch as any).mockResolvedValue(htmlResponse('<label><input type="checkbox" disabled><span>unavailable co</span></label>'));

      const ok = await m._addComposerVendor({ vendor_name: 'Unavailable Co' });

      expect(ok).toBe(true);
      expect(container.children.length).toBe(1);
    });

    it('a 4xx response keeps the inline create-form values and reports an error', async () => {
      const container = addContainer();
      const m = makeModal([], [1]);
      m.addingVendor = true;
      m.newVendorName = 'Acme Components';
      m.newVendorWebsite = 'acme.example';
      m.newVendorEmail = 'not-an-email';
      (fetch as any).mockResolvedValue(htmlResponse('', false, 400));

      await m.createVendor();

      expect(m.newVendorName).toBe('Acme Components'); // typed values preserved
      expect(m.newVendorWebsite).toBe('acme.example');
      expect(m.newVendorEmail).toBe('not-an-email');
      expect(m.addingVendor).toBe(true); // form stays open
      expect(m.addingVendorBusy).toBe(false);
      expect(container.children.length).toBe(0);
      expect(m.$store.toast.type).toBe('error');
      expect(m.$store.toast.message).toBe('Could not add vendor — please try again');
    });

    it('a network failure also keeps the create-form values', async () => {
      addContainer();
      const m = makeModal([], [1]);
      m.addingVendor = true;
      m.newVendorName = 'Acme Components';
      (fetch as any).mockRejectedValue(new Error('offline'));

      await m.createVendor();

      expect(m.newVendorName).toBe('Acme Components');
      expect(m.addingVendor).toBe(true);
      expect(m.addingVendorBusy).toBe(false);
      expect(m.$store.toast.type).toBe('error');
    });

    it('createVendor success appends the row, clears the form, and closes the panel', async () => {
      const container = addContainer();
      const m = makeModal([], [1]);
      m.addingVendor = true;
      m.newVendorName = 'Acme Components';
      m.newVendorWebsite = 'https://acme.example';
      m.newVendorEmail = 'sales@acme.example';
      (fetch as any).mockResolvedValue(htmlResponse(rowHtml('acme components')));

      await m.createVendor();

      expect(container.children.length).toBe(1);
      expect(m.newVendorName).toBe('');
      expect(m.newVendorWebsite).toBe('');
      expect(m.newVendorEmail).toBe('');
      expect(m.addingVendor).toBe(false);
      expect(m.addingVendorBusy).toBe(false);
    });

    it('addContactFor pre-fills the inline-form name, reveals the form, and focuses the email input', () => {
      const m = makeModal([], [1]);
      const emailInput = document.createElement('input');
      m.$refs = { ...m.$refs, newVendorEmail: emailInput };
      m.$nextTick = (cb: () => void) => cb();
      const focusSpy = vi.spyOn(emailInput, 'focus');

      m.addContactFor('Cyclops Cardless');

      expect(m.newVendorName).toBe('Cyclops Cardless');
      expect(m.addingVendor).toBe(true);
      expect(focusSpy).toHaveBeenCalled();
    });

    it('addContactFor does NOT overwrite a non-empty newVendorName — L2', () => {
      // L2: a buyer half-typing a manual entry, then clicking "Add contact" on a
      // suggested row, must keep their in-progress name — only reveal + focus the form.
      const m = makeModal([], [1]);
      const emailInput = document.createElement('input');
      m.$refs = { ...m.$refs, newVendorEmail: emailInput };
      m.$nextTick = (cb: () => void) => cb();
      const focusSpy = vi.spyOn(emailInput, 'focus');
      m.newVendorName = 'Half Typed Co';

      m.addContactFor('Cyclops Cardless');

      expect(m.newVendorName).toBe('Half Typed Co'); // preserved, not clobbered
      expect(m.addingVendor).toBe(true); // form still revealed
      expect(focusSpy).toHaveBeenCalled(); // email still focused
    });

    it('addContactFor fills the name when the form is whitespace-only — L2', () => {
      // A whitespace-only field counts as empty, so the suggested name fills it.
      const m = makeModal([], [1]);
      const emailInput = document.createElement('input');
      m.$refs = { ...m.$refs, newVendorEmail: emailInput };
      m.$nextTick = (cb: () => void) => cb();
      m.newVendorName = '   ';

      m.addContactFor('Cyclops Cardless');

      expect(m.newVendorName).toBe('Cyclops Cardless');
    });

    it('_rowVendorName parses the tojson-quoted normalized name (and escapes)', () => {
      const m = makeModal([], [1]);
      expect(m._rowVendorName(rowHtml('digi-key'))).toBe('digi-key');
      expect(m._rowVendorName(rowHtml("o'brien & co"))).toBe("o'brien & co");
      expect(m._rowVendorName('<label><span>excluded</span></label>')).toBeNull();
    });

    it('_rowVendorName reads data-vendor-norm from excluded rows (no x-init)', () => {
      // F11: composer_vendor_row.html emits the normalized name on excluded rows
      // as a data attribute, since they render no x-init to parse it from.
      const m = makeModal([], [1]);
      const excluded = '<label data-vendor-norm="dead vendor"><input type="checkbox" disabled><span>Dead Vendor</span></label>';
      expect(m._rowVendorName(excluded)).toBe('dead vendor');
    });

    it('dedupes a re-picked EXCLUDED vendor against rows already in the container', async () => {
      // F11: excluded rows never join selectedVendors (disabled checkbox), so the
      // selection-state dedupe can't see them — the container check must.
      const container = addContainer();
      const m = makeModal([], [1]);
      const excludedRow = '<label data-vendor-norm="dead vendor"><input type="checkbox" disabled><span>Dead Vendor</span></label>';
      (fetch as any).mockResolvedValue(htmlResponse(excludedRow));

      const first = await m._addComposerVendor({ vendor_name: 'Dead Vendor' });
      const second = await m._addComposerVendor({ vendor_name: 'Dead Vendor' });

      expect(first).toBe(true);
      expect(second).toBe(true);
      expect(container.children.length).toBe(1); // no duplicate excluded row
      expect(m.$store.toast.message).toBe('Vendor already added');
    });

    it('createVendor payloads carrying email/website bypass the already-selected fast-path', async () => {
      // F4: the server attaches a typed email to the matched existing card —
      // skipping the request would silently discard it. Bare picks still skip.
      addContainer();
      const m = makeModal(['known vendor'], [1]);
      (fetch as any).mockResolvedValue(htmlResponse(rowHtml('known vendor')));

      await m._addComposerVendor({ vendor_name: 'Known Vendor', email: 'x@known.com' });

      expect(fetch).toHaveBeenCalledTimes(1); // request went out despite the name match
      expect(m.$store.toast.type).toBe('info'); // row itself still deduped on return
    });

    it('a 4xx with a JSON error body surfaces the server reason in the toast', async () => {
      // F8: the server emits {"error": ...} — show the actionable reason, not a
      // generic try-again.
      addContainer();
      const m = makeModal([], [1]);
      (fetch as any).mockResolvedValue({
        ok: false,
        status: 400,
        json: () => Promise.resolve({ error: 'invalid website — could not extract a domain' }),
        text: () => Promise.resolve(''),
      });

      const ok = await m._addComposerVendor({ vendor_name: 'Bad Site Co', website: 'no-dot' });

      expect(ok).toBe(false);
      expect(m.$store.toast.type).toBe('error');
      expect(m.$store.toast.message).toContain('invalid website — could not extract a domain');
    });

    it('a 5xx never surfaces body text — generic try-again toast', async () => {
      addContainer();
      const m = makeModal([], [1]);
      (fetch as any).mockResolvedValue({
        ok: false,
        status: 500,
        json: () => Promise.resolve({ error: 'Internal Server Error' }),
        text: () => Promise.resolve(''),
      });

      const ok = await m._addComposerVendor({ vendor_name: 'Acme' });

      expect(ok).toBe(false);
      expect(m.$store.toast.message).toBe('Could not add vendor — please try again');
    });
  });

  describe('searchVendors failure visibility (F9)', () => {
    it('toasts ONCE per failure streak, then again after a success resets the flag', async () => {
      const m = makeModal([], [1]);
      const toastSpy = vi.spyOn(m, '_toast');
      m.vendorQuery = 'arr';
      (fetch as any).mockRejectedValue(new Error('offline'));

      await m.searchVendors();
      await m.searchVendors(); // repeated debounce failure — no second toast

      expect(toastSpy).toHaveBeenCalledTimes(1);
      expect(toastSpy).toHaveBeenCalledWith(expect.stringContaining('search failed'), 'error');
      expect(m.vendorResults).toEqual([]);
      expect(m.searchOpen).toBe(false);

      (fetch as any).mockResolvedValue({
        ok: true,
        json: () => Promise.resolve([{ name: 'Arrow', type: 'vendor' }]),
      });
      await m.searchVendors(); // success resets the flag
      (fetch as any).mockRejectedValue(new Error('offline again'));
      await m.searchVendors();

      expect(toastSpy).toHaveBeenCalledTimes(2);
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

    it('distinguishes no-email (skipped) from failed in the partial message', () => {
      const m = makeModal(['a'], [1]);
      // 1 sent of 3: 2 skipped (no email), 0 failed
      expect(m._sendOutcome(1, 3, 2).message).toBe('Sent to 1 of 3 vendors — 2 had no email');
      // 1 sent of 3: 1 skipped + 1 failed
      expect(m._sendOutcome(1, 3, 1).message).toBe('Sent to 1 of 3 vendors — 1 failed, 1 had no email');
      // 1 sent of 3: 0 skipped, 2 failed (default skipped=0)
      expect(m._sendOutcome(1, 3).message).toBe('Sent to 1 of 3 vendors — 2 failed');
    });

    it('subtracts unavailable vendors from the failed bucket (F3)', () => {
      const m = makeModal(['a'], [1]);
      // 1 sent of 3: 2 marked unavailable, 0 failed — none are "failed"
      expect(m._sendOutcome(1, 3, 0, 2).message).toBe('Sent to 1 of 3 vendors — 2 marked unavailable');
      // 1 sent of 4: 1 skipped, 1 unavailable, 1 genuinely failed — all three reasons
      expect(m._sendOutcome(1, 4, 1, 1).message).toBe(
        'Sent to 1 of 4 vendors — 1 failed, 1 had no email, 1 marked unavailable',
      );
    });
  });
});
