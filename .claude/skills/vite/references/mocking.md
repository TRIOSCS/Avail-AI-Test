# Vite — Mocking Reference (Vitest)

## Contents
- What to mock vs. test directly
- Mocking window.location
- Mocking HTMX
- Mocking Alpine stores
- Mocking fetch
- DO/DON'T pairs

## What to Mock vs. Test Directly

| Thing | Approach |
|-------|----------|
| Alpine store shape | Extract factory, test pure object |
| Alpine reactivity | Don't — use Playwright E2E |
| HTMX events/ajax | `vi.fn()` on `global.htmx` |
| `window.location` | `Object.defineProperty` in `beforeEach` |
| `fetch` | `vi.stubGlobal('fetch', vi.fn())` |
| DOM manipulation | jsdom (auto-configured in `vitest.config.ts`) |

## Mocking `window.location`

jsdom does not allow direct `window.location =` assignment. Use `Object.defineProperty`:

```ts
beforeEach(() => {
  Object.defineProperty(window, 'location', {
    value: new URL('http://localhost/v2/materials?commodity=capacitors'),
    writable: true,
    configurable: true,
  });
});

it('reads commodity from URL', () => {
  window.location.search = '?commodity=capacitors';
  filter.syncFromURL();
  expect(filter.commodity).toBe('capacitors');
});
```

Reset in `beforeEach` to prevent cross-test pollution. URL state leaks between tests
if not reset and causes order-dependent failures.

## Mocking HTMX

Components call `htmx.ajax(...)` to trigger requests after state changes. In Vitest,
stub it so tests don't fire real HTTP:

```ts
import { vi, beforeEach } from 'vitest';

beforeEach(() => {
  // htmx_app.js assigns window.htmx = htmx; in jsdom window === global
  global.htmx = { ajax: vi.fn(), on: vi.fn(), off: vi.fn() };
});

it('triggers htmx.ajax on filter apply', () => {
  component.applyFilters();
  expect(global.htmx.ajax).toHaveBeenCalledWith(
    'GET',
    expect.stringContaining('/v2/materials'),
    expect.any(Object)
  );
});
```

## Mocking Alpine Stores

Alpine stores (`Alpine.store(...)`) are registered at runtime by `htmx_app.js`. In
Vitest, mock them on `global.Alpine`:

```ts
beforeEach(() => {
  const stores: Record<string, any> = {
    toast: { message: '', type: 'info', show: false },
    sidebar: { open: true, collapsed: false },
    preferences: { resultsPerPage: 25, compactTables: false },
  };
  global.Alpine = { store: (name: string) => stores[name] } as any;
});

it('reads resultsPerPage from preferences store', () => {
  expect(component.pageSize()).toBe(25);
});
```

## Mocking `fetch`

Use `vi.stubGlobal` for fetch mocking — cleaner than `vi.spyOn` for global replacement:

```ts
import { vi, afterEach } from 'vitest';

afterEach(() => { vi.unstubAllGlobals(); });

it('handles fetch failure gracefully', async () => {
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('Network error')));

  await expect(component.loadVendors()).rejects.toThrow('Network error');
});

it('parses vendor list from response', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ items: [{ id: 1, name: 'Arrow' }], total: 1 }),
  }));

  const vendors = await component.loadVendors();
  expect(vendors).toHaveLength(1);
});
```

Always call `vi.unstubAllGlobals()` in `afterEach` — stubs persist across tests if not
cleared, causing false positives in unrelated test cases.

## DO/DON'T Pairs

**NEVER** import `htmx.org` or `alpinejs` directly in test files — they assume a real
browser DOM and `Alpine.start()` will throw. Test pure logic extracted from the
component factory instead.

**DO** use `vi.fn()` for callbacks registered with `htmx.on('htmx:afterRequest', ...)`.
Verifying the callback was registered is sufficient; testing HTMX internals is not your
job.

**NEVER** use `jest.mock()` — this codebase uses Vitest (`vi.mock()`). The APIs are
similar but `jest.mock` is not available in Vitest and will throw at runtime.

**DO** prefer `vi.stubGlobal` over monkey-patching (`global.X = ...`) — stubbing
tracks the change and restores it automatically with `vi.unstubAllGlobals()`.
