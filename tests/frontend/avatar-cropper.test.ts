/**
 * avatar-cropper.test.ts — Vitest unit tests for the avatarCropper upload path
 * in app/static/htmx_app.js. Regression anchor: the cropper POSTs via a RAW
 * fetch (not htmx), so it must add the x-csrftoken double-submit header itself
 * — without it starlette_csrf 403s every upload before the route runs and the
 * user only ever saw a generic failure message. Also covers the honest
 * HTTP-status fallback error and the success-path event bridging.
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

// Per-name store map so Alpine.store('toast') etc. get real objects.
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

let factory: (postUrl?: string, maxBytes?: number) => any;

beforeAll(async () => {
  await import('../../app/static/htmx_app.js');
  const call = (alpineMock.data.mock.calls as any[]).find((c) => c[0] === 'avatarCropper');
  expect(call, 'avatarCropper must be registered via Alpine.data').toBeTruthy();
  factory = call[1];
});

const makeComp = () => factory('/api/user/avatar', 2 * 1024 * 1024);
const smallBlob = () => new Blob([new Uint8Array(100)], { type: 'image/jpeg' });

describe('avatarCropper.upload', () => {
  beforeEach(() => {
    document.cookie = 'csrftoken=test-token-123';
    vi.unstubAllGlobals();
  });

  it('sends the x-csrftoken double-submit header on the raw fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, headers: { get: () => null } });
    vi.stubGlobal('fetch', fetchMock);
    const comp = makeComp();
    comp.upload(smallBlob(), 'image/jpeg', 'jpg');
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/user/avatar');
    expect(opts.headers['x-csrftoken']).toBe('test-token-123');
    expect(opts.headers['HX-Request']).toBe('true');
    expect(opts.credentials).toBe('same-origin');
  });

  it('surfaces the {"error"} body of a rejected upload in the modal', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ error: 'Avatar must be 2 MB or smaller.' }),
    });
    vi.stubGlobal('fetch', fetchMock);
    const comp = makeComp();
    comp.busy = true; // save() sets busy before calling upload(); mirror that
    comp.upload(smallBlob(), 'image/jpeg', 'jpg');
    await vi.waitFor(() => expect(comp.busy).toBe(false));
    expect(comp.error).toBe('Avatar must be 2 MB or smaller.');
  });

  it('falls back to an HTTP-status message when the error body is not JSON (e.g. a CSRF 403)', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: () => Promise.reject(new Error('not json')),
    });
    vi.stubGlobal('fetch', fetchMock);
    const comp = makeComp();
    comp.busy = true; // save() sets busy before calling upload(); mirror that
    comp.upload(smallBlob(), 'image/jpeg', 'jpg');
    await vi.waitFor(() => expect(comp.busy).toBe(false));
    expect(comp.error).toBe('Upload failed (HTTP 403). Try again.');
  });

  it('on success bridges avatar-updated + showToast from HX-Trigger and closes the modal', async () => {
    const trigger = JSON.stringify({
      avatarUpdated: { filename: 'user_1_abc.jpg' },
      showToast: { message: 'Profile photo updated.', type: 'success' },
    });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: (h: string) => (h === 'HX-Trigger' ? trigger : null) },
    });
    vi.stubGlobal('fetch', fetchMock);
    const winEvents: any[] = [];
    const onAvatar = (e: any) => winEvents.push(e.detail);
    window.addEventListener('avatar-updated', onAvatar);
    const bodyEvents: any[] = [];
    const onToast = (e: any) => bodyEvents.push(e.detail);
    document.body.addEventListener('showToast', onToast);
    try {
      const comp = makeComp();
      comp.open = true;
      comp.busy = true; // save() sets busy before calling upload(); mirror that
      comp.upload(smallBlob(), 'image/jpeg', 'jpg');
      await vi.waitFor(() => expect(comp.busy).toBe(false));
      expect(winEvents[0]).toEqual({ filename: 'user_1_abc.jpg' });
      expect(bodyEvents[0]).toEqual({ message: 'Profile photo updated.', type: 'success' });
      expect(comp.open).toBe(false);
    } finally {
      window.removeEventListener('avatar-updated', onAvatar);
      document.body.removeEventListener('showToast', onToast);
    }
  });

  it('rejects an over-limit blob client-side without calling fetch', () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const comp = makeComp();
    comp.upload(new Blob([new Uint8Array(2 * 1024 * 1024 + 1)]), 'image/jpeg', 'jpg');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(comp.error).toBe('The cropped image is still over 2 MB. Try a smaller photo.');
    expect(comp.busy).toBe(false);
  });
});
