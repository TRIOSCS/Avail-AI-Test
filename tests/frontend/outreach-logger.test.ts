/**
 * outreach-logger.test.ts — Vitest unit tests for the REAL [data-outreach-log]
 * delegated click listener in app/static/htmx_app.js.
 *
 * Imports htmx_app.js once (with htmx/Alpine mocked) so the single delegated
 * body listener is exercised against the shipped code: dataset→payload
 * coercion (parseInt with empty-string→null fallback), the error-toast paths
 * for !resp.ok and network failure, the dropped_links warning downgrade, and
 * the offset-preserving #cdm-list refresh via htmx.ajax.
 *
 * Called by: npx vitest run
 * Depends on: vitest, jsdom, app/static/htmx_app.js
 */

import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';

// Mock htmx and all Alpine plugins so htmx_app.js imports cleanly in jsdom.
vi.mock('htmx.org', () => ({
  default: {
    on: vi.fn(), off: vi.fn(), ajax: vi.fn(), process: vi.fn(), trigger: vi.fn(),
    defineExtension: vi.fn(), createExtension: vi.fn(), config: {},
  },
}));

const toast = { message: '', type: '', show: false };
const alpineMock = {
  data: vi.fn(),
  store: vi.fn(() => toast),
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

const fetchMock = vi.fn();

// Import ONCE — the module registers a single delegated click listener on
// document.body; re-importing per test would stack duplicate listeners.
beforeAll(async () => {
  vi.stubGlobal('fetch', fetchMock);
  await import('../../app/static/htmx_app.js');
});

function jsonResponse(body: Record<string, unknown>, ok = true, status = 201) {
  return { ok, status, json: async () => body };
}

function mountLink(attrs: Record<string, string>) {
  const a = document.createElement('a');
  a.setAttribute('data-outreach-log', '');
  for (const [k, v] of Object.entries(attrs)) a.setAttribute(k, v);
  // Prevent jsdom's "navigation not implemented" noise; the delegated body
  // listener still runs (it does not check defaultPrevented).
  a.addEventListener('click', (e) => e.preventDefault());
  document.body.appendChild(a);
  return a;
}

function click(el: HTMLElement) {
  el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

async function flush() {
  // The handler chains fetch().then(async …) — flush a few microtask turns.
  for (let i = 0; i < 6; i++) await Promise.resolve();
}

beforeEach(() => {
  document.body.innerHTML = '';
  document.cookie = 'csrftoken=testtoken';
  toast.message = '';
  toast.type = '';
  toast.show = false;
  fetchMock.mockReset();
  (htmx.ajax as any).mockReset();
  (htmx.ajax as any).mockResolvedValue(undefined);
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('[data-outreach-log] delegated click listener', () => {
  it('POSTs the coerced payload: int ids, null for empty/missing ids', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 1, dropped_links: [] }));
    const a = mountLink({
      'data-channel': 'phone',
      'data-value': '+14155551234',
      'data-company-id': '7',
      'data-site-id': '',        // legacy contact rows render empty site/contact ids
      'data-contact-name': 'Pat Buyer',
    });
    click(a);
    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/activity/outreach-initiated');
    expect(opts.method).toBe('POST');
    expect(opts.headers['x-csrftoken']).toBe('testtoken');
    expect(JSON.parse(opts.body)).toEqual({
      channel: 'phone',
      contact_value: '+14155551234',
      company_id: 7,
      customer_site_id: null,
      site_contact_id: null,
      contact_name: 'Pat Buyer',
      origin: 'cdm_workspace',
    });
  });

  it('ignores clicks outside [data-outreach-log] elements', async () => {
    const plain = document.createElement('a');
    document.body.appendChild(plain);
    click(plain);
    await flush();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows the rate-limit error toast on 429', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, false, 429));
    click(mountLink({ 'data-channel': 'email', 'data-value': 'pat@x.com' }));
    await flush();
    expect(toast.type).toBe('error');
    expect(toast.message).toContain('NOT logged');
    expect(toast.message).toContain('rate limit');
    expect(toast.show).toBe(true);
  });

  it('shows a status-bearing error toast on 500', async () => {
    fetchMock.mockResolvedValue(jsonResponse({}, false, 500));
    click(mountLink({ 'data-channel': 'email', 'data-value': 'pat@x.com' }));
    await flush();
    expect(toast.type).toBe('error');
    expect(toast.message).toContain('error 500');
  });

  it('shows the network-error toast AND logs the error on fetch rejection', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));
    click(mountLink({ 'data-channel': 'phone', 'data-value': '+14155551234' }));
    await flush();
    expect(toast.type).toBe('error');
    expect(toast.message).toContain('network error');
    expect(console.error).toHaveBeenCalledWith('[outreach-log] failed', expect.any(TypeError));
  });

  it('on success: success toast + offset-preserving #cdm-list refresh', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 9, dropped_links: [] }));
    document.body.insertAdjacentHTML(
      'beforeend',
      '<form id="cdm-filters"><span class="htmx-indicator"></span></form>' +
        '<div id="cdm-list"><div data-offset="50" data-limit="50"></div></div>'
    );
    click(mountLink({ 'data-channel': 'phone', 'data-value': '+14155551234', 'data-contact-name': 'Pat Buyer' }));
    await flush();

    expect(toast.type).toBe('success');
    expect(toast.message).toBe('Call logged — Pat Buyer');
    expect(htmx.ajax).toHaveBeenCalledWith(
      'GET',
      '/v2/partials/customers/account-list?offset=50&limit=50',
      expect.objectContaining({ source: '#cdm-filters', target: '#cdm-list' })
    );
  });

  it('skips the refresh when not on the CDM workspace', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 9, dropped_links: [] }));
    click(mountLink({ 'data-channel': 'email', 'data-value': 'pat@x.com' }));
    await flush();
    expect(toast.type).toBe('success');
    expect(htmx.ajax).not.toHaveBeenCalled();
  });

  it('downgrades to a warning toast (no refresh) when the server dropped links', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 9, dropped_links: ['contact'] }));
    document.body.insertAdjacentHTML(
      'beforeend',
      '<form id="cdm-filters"></form><div id="cdm-list"><div data-offset="0" data-limit="50"></div></div>'
    );
    click(mountLink({ 'data-channel': 'phone', 'data-value': '+14155551234' }));
    await flush();

    expect(toast.type).toBe('warning');
    expect(toast.message).toContain('contact');
    expect(toast.message).toContain('no longer exists');
    expect(htmx.ajax).not.toHaveBeenCalled();
  });

  it('a post-success rendering error is contained — never a false "NOT logged" toast', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 9, dropped_links: [] }));
    (htmx.ajax as any).mockImplementation(() => {
      throw new Error('htmx exploded');
    });
    document.body.insertAdjacentHTML(
      'beforeend',
      '<form id="cdm-filters"></form><div id="cdm-list"><div data-offset="0" data-limit="50"></div></div>'
    );
    click(mountLink({ 'data-channel': 'phone', 'data-value': '+14155551234' }));
    await flush();

    // The POST succeeded — the success toast stands; the rendering error is
    // logged for forensics instead of masquerading as a transport failure.
    expect(toast.type).toBe('success');
    expect(toast.message).not.toContain('NOT logged');
    expect(console.error).toHaveBeenCalledWith(
      '[outreach-log] post-success UI update failed',
      expect.any(Error)
    );
  });
});
