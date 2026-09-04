/**
 * unified-req-modal-description.test.ts — Vitest unit tests for the REAL
 * unifiedReqModal AI-description methods in app/static/htmx_app.js.
 *
 * Imports app/static/htmx_app.js (with htmx/Alpine mocked) and pulls the actual
 * Alpine.data('unifiedReqModal', ...) factory out of the captured registry, so
 * standardizeDescription / generateDescription are exercised against the shipped
 * code. Pins the CSRF regression: both hit /api/ai/* by raw fetch, and those paths
 * are NOT in main.py's CSRF_EXEMPT_URLS — without the x-csrftoken header
 * starlette_csrf 403s the session POST and the `if (resp.ok)` guard swallows it,
 * so the buttons silently do nothing in production.
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom, app/static/htmx_app.js
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

let registry: Record<string, any> = {};

// Mock htmx and all Alpine plugins so htmx_app.js imports cleanly in jsdom.
vi.mock('htmx.org', () => ({
  default: {
    on: vi.fn(), off: vi.fn(), ajax: vi.fn(), process: vi.fn(), trigger: vi.fn(),
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

/** Build a real factory instance (the factory takes no args). */
function makeModal() {
  return registry['unifiedReqModal']();
}

/** A part row shaped like _makePart's output, with only the fields these methods read. */
function makePart(over: Record<string, string> = {}) {
  return { primary_mpn: '', manufacturer: '', description: '', ...over };
}

function jsonResponse(body: Record<string, unknown>, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

beforeEach(async () => {
  registry = {};
  document.body.innerHTML = '';
  document.cookie = 'csrftoken=testtoken';
  vi.stubGlobal('fetch', vi.fn());
  vi.resetModules();
  await import('../../app/static/htmx_app.js');
});

describe('unifiedReqModal AI description fetches (real factory)', () => {
  describe('standardizeDescription', () => {
    it('sends the x-csrftoken header — /api/ai/standardize-description is not CSRF-exempt', async () => {
      const m = makeModal();
      (fetch as any).mockResolvedValue(jsonResponse({ description: 'IC MCU 32-BIT' }));
      const part = makePart({ description: 'some mcu chip', primary_mpn: 'STM32F407', manufacturer: 'ST' });

      await m.standardizeDescription(part);

      const [url, opts] = (fetch as any).mock.calls[0];
      expect(url).toBe('/api/ai/standardize-description');
      expect(opts.method).toBe('POST');
      expect(opts.headers['x-csrftoken']).toBe('testtoken');
      expect(opts.headers['Content-Type']).toBe('application/json');
      expect(JSON.parse(opts.body)).toEqual({
        description: 'some mcu chip',
        mpn: 'STM32F407',
        manufacturer: 'ST',
      });
      expect(part.description).toBe('IC MCU 32-BIT');
    });

    it('does not fetch at all when the description is too short and there is no MPN', async () => {
      const m = makeModal();
      await m.standardizeDescription(makePart({ description: 'ab' }));
      expect(fetch).not.toHaveBeenCalled();
    });

    it('falls back to generateDescription — which also carries the header', async () => {
      const m = makeModal();
      (fetch as any).mockResolvedValue(jsonResponse({ description: 'IC MCU', confidence: 0.9 }));
      const part = makePart({ description: '', primary_mpn: 'STM32F407', manufacturer: 'ST' });

      await m.standardizeDescription(part);

      const [url, opts] = (fetch as any).mock.calls[0];
      expect(url).toBe('/api/ai/generate-description');
      expect(opts.headers['x-csrftoken']).toBe('testtoken');
    });
  });

  describe('generateDescription', () => {
    it('sends the x-csrftoken header — /api/ai/generate-description is not CSRF-exempt', async () => {
      const m = makeModal();
      (fetch as any).mockResolvedValue(jsonResponse({ description: 'IC MCU 32-BIT', confidence: 0.9 }));
      const part = makePart({ primary_mpn: 'STM32F407', manufacturer: 'ST', description: 'old' });

      await m.generateDescription(part);

      const [url, opts] = (fetch as any).mock.calls[0];
      expect(url).toBe('/api/ai/generate-description');
      expect(opts.method).toBe('POST');
      expect(opts.headers['x-csrftoken']).toBe('testtoken');
      expect(JSON.parse(opts.body)).toEqual({
        mpn: 'STM32F407',
        manufacturer: 'ST',
        existing_description: 'old',
      });
      expect(part.description).toBe('IC MCU 32-BIT');
    });

    it('does not fetch when the MPN is too short', async () => {
      const m = makeModal();
      await m.generateDescription(makePart({ primary_mpn: 'ab' }));
      expect(fetch).not.toHaveBeenCalled();
    });

    it('leaves the description alone when confidence is below 0.75', async () => {
      const m = makeModal();
      (fetch as any).mockResolvedValue(jsonResponse({ description: 'GUESS', confidence: 0.4 }));
      const part = makePart({ primary_mpn: 'STM32F407', description: 'keep me' });

      await m.generateDescription(part);

      expect(part.description).toBe('keep me');
    });
  });

  it('sends an empty x-csrftoken rather than omitting it when no cookie is set', async () => {
    // csrfToken() returns '' when the cookie is missing; the header must still be
    // present and wired to the helper, so the assertion pins the wiring, not the value.
    document.cookie = 'csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    const m = makeModal();
    (fetch as any).mockResolvedValue(jsonResponse({ description: 'X' }));

    await m.standardizeDescription(makePart({ description: 'some mcu chip' }));

    const [, opts] = (fetch as any).mock.calls[0];
    expect(opts.headers).toHaveProperty('x-csrftoken');
  });
});
