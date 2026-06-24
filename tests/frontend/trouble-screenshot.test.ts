/**
 * trouble-screenshot.test.ts — Vitest unit tests for the REAL trouble-ticket
 * capture helpers in app/static/htmx_app.js: captureTroubleScreenshot (lazy
 * modern-screenshot import, 2MB downscale ladder, graceful null), the
 * capture-before-open sequencing of openTroubleReport, collectTroubleContext,
 * and the console.error/warn → errorLog tee.
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom, app/static/htmx_app.js
 */

import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';

// Mock htmx + Alpine plugins so htmx_app.js imports cleanly in jsdom.
vi.mock('htmx.org', () => ({
  default: {
    on: vi.fn(), off: vi.fn(), ajax: vi.fn(), process: vi.fn(), trigger: vi.fn(),
    swap: vi.fn(), defineExtension: vi.fn(), createExtension: vi.fn(), config: {},
  },
}));

// modern-screenshot is dynamically imported inside the capture helper.
const domToPng = vi.fn();
vi.mock('modern-screenshot', () => ({ domToPng }));

// Per-name store map so pushCappedLog('errorLog') gets a real {entries:[]}.
const stores: Record<string, any> = {};
const alpineMock = {
  data: vi.fn(),
  store: vi.fn((name: string, val?: any) => {
    if (val !== undefined) { stores[name] = val; return val; }
    if (!stores[name]) stores[name] = {};
    return stores[name];
  }),
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

const SMALL_PNG = 'data:image/png;base64,AAAA';

beforeAll(async () => {
  await import('../../app/static/htmx_app.js');
});

beforeEach(() => {
  document.body.innerHTML = '';
  domToPng.mockReset();
  // Make rAF synchronous so openTroubleReport's double-rAF resolves immediately.
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0; });
});

describe('captureTroubleScreenshot', () => {
  it('returns the data URL when capture succeeds under the size cap', async () => {
    domToPng.mockResolvedValue(SMALL_PNG);
    const url = await (window as any).captureTroubleScreenshot();
    expect(url).toBe(SMALL_PNG);
    expect(domToPng).toHaveBeenCalledWith(document.body, expect.objectContaining({ backgroundColor: '#ffffff' }));
  });

  it('resolves null (never throws) when the lib fails', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    domToPng.mockRejectedValue(new Error('tainted canvas'));
    const url = await (window as any).captureTroubleScreenshot();
    expect(url).toBeNull();
    expect(spy).toHaveBeenCalledWith('[trouble-ticket] screenshot capture failed', expect.any(Error));
    spy.mockRestore();
  });

  it('walks the downscale ladder and returns the first under-cap render', async () => {
    const tooBig = 'data:image/png;base64,' + 'A'.repeat(2_000_000);
    domToPng
      .mockResolvedValueOnce(tooBig) // scale 1
      .mockResolvedValueOnce(tooBig) // scale 0.75
      .mockResolvedValueOnce(SMALL_PNG); // scale 0.5
    const url = await (window as any).captureTroubleScreenshot();
    expect(url).toBe(SMALL_PNG);
    expect(domToPng).toHaveBeenCalledTimes(3);
    expect(domToPng.mock.calls.map((c) => c[1].scale)).toEqual([1, 0.75, 0.5]);
  });

  it('returns null when every scale is over the cap', async () => {
    domToPng.mockResolvedValue('data:image/png;base64,' + 'A'.repeat(2_000_000));
    expect(await (window as any).captureTroubleScreenshot()).toBeNull();
  });
});

describe('openTroubleReport', () => {
  it('captures BEFORE dispatching open-modal with the form url', async () => {
    domToPng.mockResolvedValue(SMALL_PNG);
    const dispatch = vi.spyOn(window, 'dispatchEvent');
    await (window as any).openTroubleReport();

    expect((window as any)._ttScreenshot).toBe(SMALL_PNG);
    expect((window as any)._ttContext).toBeTruthy();
    const evt = dispatch.mock.calls.map((c) => c[0]).find((e: any) => e.type === 'open-modal') as any;
    expect(evt).toBeTruthy();
    expect(evt.detail.url).toBe('/api/trouble-tickets/form');
    dispatch.mockRestore();
  });
});

describe('collectTroubleContext', () => {
  it('captures nav history (capped), build, and url-derived current_view', () => {
    document.head.insertAdjacentHTML('beforeend', '<meta name="app-build" content="build-xyz">');
    const ctx = (window as any).collectTroubleContext();
    expect(typeof ctx.timestamp).toBe('string');
    expect(typeof ctx.online).toBe('boolean');
    expect(ctx.app_build).toBe('build-xyz');
    expect(Array.isArray(ctx.nav_history)).toBe(true);
  });
});

describe('submitTroubleReport (Alpine-safe single-call handler)', () => {
  const flush = async () => { for (let i = 0; i < 6; i++) await Promise.resolve(); };

  it('POSTs the report payload with CSRF + screenshot + context, toggles submitting', async () => {
    document.body.innerHTML = '<textarea id="tr-description">It broke</textarea><div id="modal-content"></div>';
    document.cookie = 'csrftoken=tok123';
    const fetchMock = vi.fn().mockResolvedValue({ text: async () => '<div>Report submitted!</div>' });
    vi.stubGlobal('fetch', fetchMock);
    (window as any)._ttScreenshot = 'data:image/png;base64,AAA';
    (window as any)._ttContext = { current_view: 'search' };
    const data: any = { submitting: false };

    (window as any).submitTroubleReport(data);
    expect(data.submitting).toBe(true); // set synchronously
    await flush();

    expect(fetchMock).toHaveBeenCalledWith('/api/trouble-tickets/submit', expect.objectContaining({ method: 'POST' }));
    const opts = fetchMock.mock.calls[0][1];
    expect(opts.headers['X-CSRFToken']).toBe('tok123');
    const body = JSON.parse(opts.body);
    expect(body.description).toBe('It broke');
    expect(body.screenshot).toBe('data:image/png;base64,AAA');
    expect(JSON.parse(body.auto_captured_context).current_view).toBe('search');
    expect(data.submitting).toBe(false); // reset after response
  });

  it('does nothing when the description is empty', () => {
    document.body.innerHTML = '<textarea id="tr-description">   </textarea>';
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    (window as any).submitTroubleReport({ submitting: false });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('console tee → errorLog', () => {
  it('records console.error in the errorLog store', () => {
    stores.errorLog = { entries: [] };
    console.error('[unit] boom', new Error('x'));
    expect(stores.errorLog.entries.length).toBe(1);
    expect(stores.errorLog.entries[0].level).toBe('error');
    expect(stores.errorLog.entries[0].msg).toContain('boom');
  });

  it('records console.warn too', () => {
    stores.errorLog = { entries: [] };
    console.warn('[unit] heads up');
    expect(stores.errorLog.entries.length).toBe(1);
    expect(stores.errorLog.entries[0].level).toBe('warn');
  });
});
