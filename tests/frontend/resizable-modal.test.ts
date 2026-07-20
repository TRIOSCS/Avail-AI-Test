/**
 * resizable-modal.test.ts — Vitest unit tests for the REAL resizableModal Alpine factory.
 *
 * Imports app/static/htmx_app.js (htmx/Alpine mocked) and pulls the actual
 * Alpine.data('resizableModal', ...) factory from the captured registry, so the new
 * window-resize re-clamp and pointercancel teardown behaviors are exercised against the
 * shipped component, not a mirror.
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom, app/static/htmx_app.js
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

let registry: Record<string, any> = {};

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

function setViewport(w: number, h: number) {
  Object.defineProperty(window, 'innerWidth', { value: w, configurable: true, writable: true });
  Object.defineProperty(window, 'innerHeight', { value: h, configurable: true, writable: true });
}

function makeModal() {
  return registry['resizableModal']();
}

beforeEach(async () => {
  registry = {};
  document.body.innerHTML = '';
  document.body.style.userSelect = '';
  // jsdom has no matchMedia — the component calls it in init().
  window.matchMedia = vi.fn().mockReturnValue({
    matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  }) as any;
  setViewport(1280, 900);
  vi.resetModules();
  await import('../../app/static/htmx_app.js');
});

describe('resizableModal — re-clamp on window resize (fix b)', () => {
  it('shrinks a floating custom panel back onto a smaller viewport', () => {
    const m = makeModal();
    m.init();
    m.isDesktop = true;
    m.custom = true;
    m.width = 1000; m.height = 800; m.left = 240; m.top = 150;

    setViewport(800, 600);
    window.dispatchEvent(new Event('resize'));

    // clampToViewport(margin=16): w=min(1000,784)=784, h=min(800,584)=584, then l/t clamped on-screen.
    expect(m.width).toBe(784);
    expect(m.height).toBe(584);
    expect(m.left).toBeLessThanOrEqual(800 - m.width);
    expect(m.top).toBeLessThanOrEqual(600 - m.height);
    expect(m.left).toBeGreaterThanOrEqual(0);
    expect(m.top).toBeGreaterThanOrEqual(0);
  });

  it('does nothing when the panel is not custom (still using the centered layout)', () => {
    const m = makeModal();
    m.init();
    m.isDesktop = true;
    m.custom = false;
    m.width = 1000; m.height = 800;

    setViewport(800, 600);
    window.dispatchEvent(new Event('resize'));

    expect(m.width).toBe(1000);
    expect(m.height).toBe(800);
  });
});

describe('resizableModal — pointercancel teardown (fix c)', () => {
  it('tears down an in-progress drag on pointercancel, clearing listeners + user-select', () => {
    const m = makeModal();
    m.init();
    m.isDesktop = true;
    m.custom = true; // skip _seed() (which needs $refs.panel)
    m.width = 600; m.height = 500; m.left = 100; m.top = 100;

    const target = { setPointerCapture: vi.fn(), releasePointerCapture: vi.fn() };
    m.startMove({ button: 0, clientX: 10, clientY: 10, pointerId: 1, preventDefault: vi.fn(), target });

    expect(m._drag).not.toBeNull();
    expect(document.body.style.userSelect).toBe('none');

    document.dispatchEvent(new Event('pointercancel'));

    expect(m._drag).toBeNull();
    expect(document.body.style.userSelect).toBe('');
    expect(target.releasePointerCapture).toHaveBeenCalled();
  });
});

describe('resizableModal — per-modal geometry buckets (edit-modal glitch fix)', () => {
  beforeEach(() => localStorage.clear());

  it('derives the bucket from the opened URL with numeric ids normalized', () => {
    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/7/contacts/32/edit-form?origin=contacts&filter_limit=50' });
    expect(m.bucket).toBe('lg:/v2/partials/customers/:id/contacts/:id/edit-form');
  });

  it('keeps wide and standard buckets for the same URL separate', () => {
    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/foo/9/picker', wide: true });
    expect(m.bucket).toBe('wide:/v2/partials/foo/:id/picker');
  });

  it('falls back to the legacy size bucket when opened without a url', () => {
    const m = makeModal();
    m.init();
    m.onOpen({});
    expect(m.bucket).toBe('lg');
  });

  it('does not apply geometry saved by a DIFFERENT modal', () => {
    localStorage.setItem(
      'avail_modal_geom',
      JSON.stringify({ 'lg:/v2/partials/other/create-form': { w: 380, h: 260, l: 900, t: 700 } }),
    );
    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/1/contacts/2/edit-form' });
    expect(m.custom).toBe(false);
  });

  it('ignores legacy shared-bucket entries (bare "lg" key) when a url is present', () => {
    localStorage.setItem('avail_modal_geom', JSON.stringify({ lg: { w: 380, h: 260, l: 900, t: 700 } }));
    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/1/contacts/2/edit-form' });
    expect(m.custom).toBe(false);
  });

  it('still restores geometry saved for the SAME modal', () => {
    localStorage.setItem(
      'avail_modal_geom',
      JSON.stringify({ 'lg:/v2/partials/customers/:id/contacts/:id/edit-form': { w: 600, h: 500, l: 100, t: 100 } }),
    );
    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/5/contacts/9/edit-form?origin=contacts' });
    expect(m.custom).toBe(true);
    expect(m.width).toBe(600);
    expect(m.height).toBe(500);
  });

  it('ignores degenerate saved sizes below the sane minimum', () => {
    localStorage.setItem(
      'avail_modal_geom',
      JSON.stringify({ 'lg:/v2/partials/customers/:id/contacts/:id/edit-form': { w: 200, h: 120, l: 10, t: 10 } }),
    );
    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/5/contacts/9/edit-form' });
    expect(m.custom).toBe(false);
  });
});

describe('resizableModal — onOpen loading behavior (edit-modal glitch fix)', () => {
  beforeEach(() => {
    localStorage.clear();
    document.body.innerHTML =
      '<div id="modal-loading"></div><div id="modal-content"><form id="stale-form"><input name="stale"></form></div>';
  });

  it('clears the previous modal content before the fetch lands', async () => {
    const htmxMod = (await import('htmx.org')).default as any;
    htmxMod.ajax.mockReturnValue(new Promise<void>(() => {})); // never resolves

    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/1/contacts/2/edit-form' });

    expect(document.getElementById('modal-content')!.children.length).toBe(0);
  });

  it('drives the spinner via htmx.ajax indicator (not a hand-rolled toggle)', async () => {
    const htmxMod = (await import('htmx.org')).default as any;
    htmxMod.ajax.mockClear();
    htmxMod.ajax.mockReturnValue(Promise.resolve());

    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/1/contacts/2/edit-form' });

    expect(htmxMod.ajax).toHaveBeenCalledWith(
      'GET',
      '/v2/partials/customers/1/contacts/2/edit-form',
      expect.objectContaining({ indicator: '#modal-loading', target: '#modal-content' }),
    );
  });

  it('focuses the first visible field of the loaded content on desktop', async () => {
    const htmxMod = (await import('htmx.org')).default as any;
    htmxMod.ajax.mockImplementation(() => {
      document.getElementById('modal-content')!.innerHTML =
        '<form><input type="hidden" name="origin"><input name="first_name"></form>';
      return Promise.resolve();
    });

    const m = makeModal();
    m.init();
    m.onOpen({ url: '/v2/partials/customers/1/contacts/2/edit-form' });
    await new Promise((r) => setTimeout(r, 0));

    expect((document.activeElement as HTMLInputElement).name).toBe('first_name');
  });
});
